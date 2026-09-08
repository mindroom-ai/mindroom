---
icon: lucide/network
---

# Personal MCP Gateway

The optional MindRoom MCP gateway exposes a personal agent's assigned tools to external MCP clients at `/mcp`.
It reuses the [Connections portal](trusted-upstream-auth.md#personal-connections-portal), personal credentials, tool filters, and worker routing.
Each user connects only the services they need.
An unconnected or unavailable integration does not prevent discovery or use of another integration.

The initial MCP tool list contains exactly three operations:

| Operation | Purpose |
|-----------|---------|
| `search_tools` | Search assigned integrations, or select a toolkit to search its functions without loading schemas into model context |
| `get_tool` | Fetch the input schema for one selected function |
| `invoke_tool` | Run that function with the authenticated user's personal connections |

For example, search with `{"query": "calendar"}`, then pass the returned `toolkit` to another search.
Use the returned `toolkit` and `function` with `get_tool` before calling `invoke_tool` with `arguments`.
If a service needs authorization, the response contains `error.code: connection_required` and a `connection_url` pointing to `/connections`.
Connect that service in the browser, then retry the selected operation.
The gateway does not require authorization to unrelated services during client login.

## Enable the gateway

First configure the Connections portal, including strict signed upstream authentication, a verified Matrix identity, and an explicitly authorized private agent.
The same `MINDROOM_CONNECTIONS_AGENT` selects the gateway's agent:

```bash
MINDROOM_CONNECTIONS_AGENT=personal
MINDROOM_PUBLIC_URL=https://assistant.example.org
MINDROOM_MCP_GATEWAY_ENABLED=true
```

The selected agent must use `private.per: user` or `private.per: user_agent`.
Access requires an explicit matching `access.users` grant or administrator authority; room membership alone is insufficient.
Both eager and deferred assigned tools are discoverable.
The client cannot choose another agent, user, or credential owner.

`MINDROOM_PUBLIC_URL` must be an HTTPS origin without a path, query, or fragment.
Loopback HTTP origins are accepted for local development.
Gateway environment changes require an API restart.
The feature is disabled by default and requires both `MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED=true` and `MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT=true`.

## Route browser and machine traffic

Forward these paths to the MindRoom API:

| Paths | Authentication at the access proxy |
|-------|------------------------------------|
| `/connections`, `/connections/*`, `/api/connections`, `/api/connections/*`, existing `/api/oauth/*` | Existing signed browser authentication |
| `/mcp` | Pass through to gateway bearer authentication |
| `/mcp/oauth/authorize`, `/mcp/oauth/register`, `/mcp/oauth/token`, `/mcp/oauth/revoke` | Public OAuth protocol endpoints; pass through to the gateway |
| `/.well-known/oauth-authorization-server/mcp/oauth`, `/.well-known/oauth-protected-resource/mcp` | Public client discovery |
| `/mcp/oauth/.well-known/oauth-authorization-server` | Compatibility discovery alias |

Browser consent lives at `/connections/mcp/authorize`, inside the existing authenticated browser prefix.
Keep that path out of another frontend's service-worker navigation fallback.
Machine endpoints must receive MCP/OAuth responses instead of an access proxy's HTML login page.
Strip client-supplied trusted identity headers at the proxy, including on machine paths.
Preserve the public `Host` header and the `Authorization`, `Origin`, `Accept`, `Content-Type`, and `MCP-Protocol-Version` headers.
Forward the exact `/mcp` path without adding a trailing slash.

Native clients can omit `Origin`.
For a browser-based MCP client, allow its exact origin separately:

```bash
MINDROOM_MCP_GATEWAY_ALLOWED_ORIGINS=https://client.example.org,http://localhost:6274
```

The gateway's public origin is allowed automatically.
Additional origins must be HTTPS or loopback HTTP; wildcards are rejected.
Gateway MCP CORS does not accept browser cookies, and its policy does not grant access to dashboard APIs.
Public client registration, token, revocation, and discovery endpoints support noncredentialed cross-origin requests.
The consent form still requires a signed browser identity, a one-use nonce, and a same-origin POST.

## Connect an MCP client

Configure a Streamable HTTP server with URL `https://assistant.example.org/mcp`.
The supported client flow uses OAuth authorization code with PKCE S256 and dynamic public-client registration:

1. Read the resource metadata URL from the gateway's `401` bearer challenge.
2. Discover the authorization server at issuer `https://assistant.example.org/mcp/oauth`.
3. Register with `token_endpoint_auth_method: none`, `response_types: [code]`, and both `authorization_code` and `refresh_token` in `grant_types`.
4. Request scope `mcp:tools` and exact resource `https://assistant.example.org/mcp`.
5. Open the authorization URL, sign in through the existing browser login, and approve the named client.
6. Exchange the code with its original callback, PKCE verifier, and exact resource, then send the issued access token as a bearer on MCP requests.

The exact resource is required for both code exchange and refresh.
Client callbacks must be registered HTTPS URLs or loopback HTTP URLs.
Dashboard API keys, browser cookies, upstream identity headers, and third-party provider tokens do not authenticate the MCP endpoint.
MindRoom issues its own client grant; upstream service tokens stay in the existing personal credential store.

This implementation uses the Python MCP SDK 1.x Streamable HTTP protocol, tested with protocol version `2025-11-25`.
It uses stateless requests and JSON responses; it does not offer resumable SSE sessions, resources, prompts, or newer protocol features outside that SDK version.
The gateway validates SDK request and notification envelopes before dispatch and returns fixed errors without logging malformed input or unknown tool names.
Valid unsolicited response/error envelopes are acknowledged with an empty HTTP 202 and dropped: this stateless gateway never sends correlated client-result requests.
If bidirectional requests or stateful sessions are added later, their client responses must instead reach the owning session.
Client applications need support for this OAuth registration and discovery flow; a static bearer-only configuration screen cannot perform initial login.

## Access and lifecycle

Access tokens last up to 15 minutes.
Refresh tokens rotate on use; replay revokes the grant family.
By default, a managed-account grant expires after 30 days without successful refresh or tool use and at most 180 days after approval.
The configured idle and absolute lifetimes determine the actual deadlines.
Successful tool discovery and calls update the portal's last-used time; token refresh extends idle expiry without pretending that a tool was used.
Portal visits, failed calls, and invalid tokens do not extend idle expiry.
Background token refresh counts as activity, so inactivity expiry alone does not bound a client that keeps refreshing.
The absolute deadline never moves, even with continued use.
The OAuth revocation endpoint revokes the whole client grant.

The Connections portal lists authorized clients with their client address, last tool use, and expiry.
Users can disconnect one client connection or all of their client connections without disconnecting their upstream service accounts.
Disconnecting all also invalidates their already-open consent requests and unexchanged authorization codes.
An authorized client may request fresh consent afterward.
Client names are self-declared; users should check the displayed client address before approving access.
Management remains available after a user loses personal-agent tool permission, using the same signed identity and exact credential-owner binding.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MINDROOM_MCP_OAUTH_IDLE_TTL_DAYS` | `30` | Maximum time without successful refresh or tool use |
| `MINDROOM_MCP_OAUTH_GRANT_TTL_DAYS` | `180` with provisioning, otherwise `30` | Absolute lifetime measured from approval |
| `MINDROOM_MCP_SCIM_TOKEN` | unset | Separate provisioning bearer secret, at least 32 characters |

Durations must be positive whole days, idle lifetime cannot exceed absolute lifetime, and absolute lifetime cannot exceed 365 days.
Lifetimes above 30 days require configured account provisioning so that account-disable updates can revoke long-lived access.
Restart the API after changing lifecycle settings.
Existing grants keep their original absolute deadlines during migration; missing historical creation times or client addresses remain unknown.
Enabling managed-account mode makes older grants without a provisioned account binding unusable; users must approve a fresh connection once.

Each MCP request rechecks current personal-agent access.
Grants retain both the original signed identity and its canonical credential owner, so alias reassignment cannot transfer an old grant to another owner or preserve access to the previous one.
Changes to the selected agent, access grants, assigned tools, and provider connections are checked before execution.

Inbound OAuth state is persisted in `mcp_gateway/oauth.sqlite3` under the configured storage root.
Keep that directory private and persistent across restarts.
Opaque authorization codes, browser nonces, access tokens, and refresh tokens are stored as hashes; upstream provider credentials remain separate.
Run one API process for this gateway: active-call limits and cancellation ownership are process-local.
Multiple independently routed replicas are not supported by this implementation.

Active MCP calls have three independently configurable concurrency limits:

| Environment variable | Default | Scope |
| --- | --- | --- |
| `MINDROOM_MCP_GATEWAY_MAX_ACTIVE_CALLS` | `128` | All active calls in the API process |
| `MINDROOM_MCP_GATEWAY_MAX_USER_CALLS` | `32` | One user across all client grants |
| `MINDROOM_MCP_GATEWAY_MAX_GRANT_CALLS` | `16` | One authorized client connection |

Each setting accepts a positive integer and takes effect after restarting the API process.
Invalid values disable the gateway; zero does not mean unlimited.
All three limits apply to `search_tools`, `get_tool`, and `invoke_tool`; the first exhausted allowance rejects a call with an MCP `busy` error before tool execution.
Excess calls do not queue or retry automatically.
Cancelled or timed-out calls retain their allowances until background work and cleanup finish.
These defaults are conservative starting values, not measured capacity; tune them against available resources, tool latency, and upstream quotas.

Public registration and authorization starts share an aggregate admission limit of 60 requests per minute per API process and a per-source limit of 10 requests per minute.
Set the positive integers `MINDROOM_MCP_GATEWAY_ONBOARDING_RATE_LIMIT` and `MINDROOM_MCP_GATEWAY_ONBOARDING_SOURCE_RATE_LIMIT` to adjust these limits.
Keep the source limit below the aggregate limit to leave capacity for other sources.
Excess requests receive HTTP 429 with `Retry-After`; token exchange, refresh, revocation, discovery, and existing MCP grants remain usable.
Requests rejected by the limiter consume neither allowance and do not extend either rate-limit window.
Admitted requests count even when later OAuth validation fails.

The source is the canonical client IP supplied by the ASGI server, without its port; the gateway does not read forwarding headers to identify callers.
Behind a proxy, configure Uvicorn's `FORWARDED_ALLOW_IPS` with only the actual trusted proxy addresses or networks, and ensure those proxies sanitize the forwarded client-address chain.
Do not trust arbitrary peers to supply that address.
Without trusted proxy normalization, callers behind that proxy share its source allowance.
Users behind the same NAT also share an allowance; missing or non-IP peer addresses use one shared fallback allowance.
These limits provide bounded admission and source fairness, not protection against a distributed denial-of-service attack.

Abandoned registrations expire 24 hours after registration; a live grant or pending consent preserves its registered client.
Anonymous reads and repeat registration do not extend that retention period.
Pending consent and client metadata without a live grant share a 64-MiB onboarding budget, including an allowance for row and index overhead.
Set the positive integer `MINDROOM_MCP_GATEWAY_ONBOARDING_MAX_BYTES` to adjust this storage budget.
At capacity, registration returns private HTTP 503 with `Retry-After: 60`; authorization returns the OAuth `temporarily_unavailable` error to its validated callback.
Expired onboarding records and inactive grant families are pruned during store write operations and client-registration lookups.
Consumed refresh token bindings remain available throughout a live grant's lifetime for revocation and refresh replay detection.
Consumed refresh metadata is compacted; expired access token bindings are reclaimed.
Revocation using an expired access token may succeed as a no-op; use the portal or a retained unexpired refresh token to disconnect the client.
This onboarding budget is separate from the durable OAuth limits below.

All retained OAuth state shares a 256-MiB logical budget, configured with the positive integer `MINDROOM_MCP_OAUTH_MAX_BYTES`.
Each requester has a 16-MiB logical budget, configured with the positive integer `MINDROOM_MCP_OAUTH_USER_MAX_BYTES`.
Accounting includes UTF-8 payloads and identifiers, a 1024-byte allowance per row for fixed fields and indexes, and conservative counter bookkeeping.
Requester usage includes its grants and capabilities plus registered client metadata charged once per grant; global usage counts the actual client row once.
Global admission applies atomically when registering clients, creating or binding consent, approving grants, and issuing tokens; requester admission applies to grant approval and token issuance.
Increasing limits admits more retained state; decreasing limits may prevent existing clients from refreshing until quota is released.
An existing database above either limit remains readable and revocable.

A grant may issue at most six token pairs in a rolling 60-second window, including its initial code exchange.
At exactly 60 seconds an issuance leaves the window; restarting does not reset issuance history or storage usage.
During the one-time schema migration, retained legacy access tokens receive the migration time as their issuance timestamp without changing expiry, so an existing family may wait up to 60 seconds before refreshing.
Token exchange and browser consent capacity failures return private HTTP 503 with `Retry-After: 60` and `temporarily_unavailable`.
Rejected issuance leaves the current refresh token or authorization code usable, and rejected consent preserves its prior nonce.
Quota recovery can take longer than the retry interval: successful revocation or expiry releases family storage, while consumed refresh-token bindings remain until their family ends.
Refresh replay still revokes the family even when its byte or issuance budget is exhausted.

These are logical retained-state limits, not physical SQLite file or filesystem quotas.
SQLite schema pages, allocation, journals, and freed pages can make physical disk use exceed the logical budget; use a filesystem or volume quota where a hard disk bound is required.
Cleanup runs before admission and remains committed when a new write is rejected.

Gateway grants retain their configured absolute deadline, with access tokens lasting at most 15 minutes and refresh unable to extend that deadline.
A current access token or any retained, unexpired refresh token can revoke its family.
Revocation using an already-invalid access token may succeed as a no-op.
Browser logout or expiry of the browser identity JWT does not automatically revoke these independent gateway grants.
Removing the user's current access policy blocks MCP use immediately.

## Managed account provisioning

Configure a dedicated `MINDROOM_MCP_SCIM_TOKEN` secret and expose `/mcp/scim/v2` to the identity provider over HTTPS.
These endpoints authenticate only the dedicated provisioning bearer and must bypass interactive browser-login redirects.
Do not share the provisioning secret with MCP clients or browser applications.
The provisioning routes do not allow browser CORS access.

The endpoint implements a restricted SCIM 2.0 User lifecycle profile: create, list, read, replace, PATCH, and delete, plus service-provider/schema discovery.
User names match verified browser email exactly, including case; schema discovery advertises this case-sensitive policy.
This differs from the general SCIM core userName case-insensitive convention, so check connector matching behavior during enrollment.
Configure the connector's `userName` attribute to the same email verified by the signed browser identity; provisioning does not authenticate the browser or grant tool permissions.
Group management, bulk operations, password management, and sorting are unsupported.
Disable group management in the connector; unsupported group requests return an explicit error.
Connectors that insist on a successful test-group operation need a compatible configuration before this endpoint can be used.

PATCH supports the top-level attributes `userName`, `active`, `displayName`, `externalId`, `name`, and `emails`, plus `name.givenName`, `name.familyName`, and `name.formatted`.
Update email entries through the whole `emails` attribute; dotted email subattributes and filtered paths such as `emails[type eq "work"].value` are unsupported and return `400 invalidPath`.
An unsupported operation rolls back the entire PATCH, including any accompanying deactivation.
Configure and test the connector's actual update and deactivation payloads against this profile before relying on provisioning for offboarding.

Use the base URL `https://assistant.example.org/mcp/scim/v2` with the dedicated bearer token in a compatible custom SCIM connector.
Provision an active test user, approve an MCP client through the existing portal login, then deactivate the provisioned user and verify that token refresh, tool use, and further consent are rejected.
Account deactivation, user-name changes, and deletion remove all bound grants and pending consent atomically.
Reactivation permits a new approval and never restores old grants.
Unknown or inactive accounts cannot authorize clients in managed-account mode.
Removing the provisioning configuration must not be used as a way to bypass managed account checks.

Deactivation takes effect when MindRoom receives and commits the provisioning update; monitor connector delivery and retries.
An upstream outage or delayed event can delay offboarding, and already-dispatched provider actions cannot be undone.
This directory governs MCP client grants; it does not deactivate Matrix accounts, erase upstream service credentials, or replace browser-session logout.
Provisioned directory records are administrator-managed and outside the OAuth logical byte budgets; apply the runtime's physical volume quota to the whole database.

## Execution limits

- Tools requiring native confirmation or configured approval cannot run through the gateway; they return `approval_required`.
- Tools requiring a live Matrix conversation are unavailable through this transport.
- MCP generic bridge dispatchers are excluded; only selected, filtered typed functions are exposed.
- Search returns at most 10 items and 16 KiB. A selected schema is limited to 32 KiB; tool arguments and result payloads to 64 KiB each.
- HTTP request bodies and MCP tool responses are limited to 128 KiB. JSON-encoded request IDs are limited to 128 bytes; the `MCP-Protocol-Version` header to 64 UTF-8 bytes. Calls have a 60-second gateway deadline, with configurable active-call limits (defaults: 16 per grant, 32 per authoritative requester across grants, and 128 per process). A cancelled or timed-out call retains its capacity until its local background work and toolkit cleanup finish, including its grant and requester allowances.
- Explicit MCP cancellation applies only to a matching request ID within the same client grant. Cancellation and timeout stop waiting, but a synchronous or remote action may already have taken effect. Do not automatically retry a potentially mutating call.

Synchronous native tool work shares the API process and cannot be forcibly stopped by request cancellation.
A stuck provider call or toolkit cleanup can retain capacity and delay graceful shutdown indefinitely; a bounded process shutdown requires termination by the deployment supervisor.
The gateway does not provide process isolation for native integrations.

The gateway does not automatically retry an invocation whose outcome is unknown.
Upstream MCP reconnection can refresh a failed session for a later call without replaying the failed action.
Existing tool-specific authorization, provider scopes, worker isolation, and filters remain authoritative.
