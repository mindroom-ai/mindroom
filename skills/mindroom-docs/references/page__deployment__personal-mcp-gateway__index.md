# Personal MCP Gateway

The optional MindRoom MCP gateway exposes a personal agent's assigned tools to external MCP clients at `/mcp`.
It reuses the [Connections portal](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#personal-connections-portal), personal credentials, tool filters, and worker routing.
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
Client applications need support for this OAuth registration and discovery flow; a static bearer-only configuration screen cannot perform initial login.

## Access and lifecycle

Access tokens last up to 15 minutes.
Refresh tokens rotate on use; replay revokes the grant family.
A grant expires at most 30 days after approval, even with refreshes.
The OAuth revocation endpoint revokes the whole client grant.

Each MCP request rechecks current personal-agent access.
Grants retain both the original signed identity and its canonical credential owner, so alias reassignment cannot transfer an old grant to another owner or preserve access to the previous one.
Changes to the selected agent, access grants, assigned tools, and provider connections are checked before execution.

Inbound OAuth state is persisted in `mcp_gateway/oauth.sqlite3` under the configured storage root.
Keep that directory private and persistent across restarts.
Opaque authorization codes, browser nonces, access tokens, and refresh tokens are stored as hashes; upstream provider credentials remain separate.
Run one API process for this gateway: active-call limits and cancellation ownership are process-local.
Multiple independently routed replicas are not supported by this implementation.

Public registration and authorization starts share an admission limit of 60 requests per minute per API process.
Set the positive integer `MINDROOM_MCP_GATEWAY_ONBOARDING_RATE_LIMIT` to adjust it.
Excess requests receive HTTP 429 with `Retry-After`; token exchange, refresh, revocation, discovery, and existing MCP grants remain usable.

Abandoned registrations expire 24 hours after registration; a live grant or pending consent preserves its registered client.
Anonymous reads and repeat registration do not extend that retention period.
Pending consent and client metadata without a live grant share a 64-MiB onboarding budget, including an allowance for row and index overhead.
Set the positive integer `MINDROOM_MCP_GATEWAY_ONBOARDING_MAX_BYTES` to adjust this storage budget.
At capacity, registration returns HTTP 503 with `Retry-After`; authorization returns the OAuth `temporarily_unavailable` error to its validated callback.
Expired onboarding records and inactive grant families are pruned during store write operations and client-registration lookups.
Access and refresh token bindings remain available throughout a live grant's lifetime for revocation and refresh replay detection.
These controls bound anonymous onboarding state; they do not impose a user or device count limit or block existing grants from refreshing or being revoked.

## Execution limits

- Tools requiring native confirmation or configured approval cannot run through the gateway; they return `approval_required`.
- Tools requiring a live Matrix conversation are unavailable through this transport.
- MCP generic bridge dispatchers are excluded; only selected, filtered typed functions are exposed.
- Search returns at most 10 items and 16 KiB. A selected schema is limited to 32 KiB; tool arguments and result payloads to 64 KiB each.
- HTTP request bodies and MCP tool responses are limited to 128 KiB. JSON-encoded request IDs are limited to 128 bytes. Calls have a 60-second gateway deadline, with at most 128 active calls per process.
- Explicit MCP cancellation applies only to a matching request ID within the same client grant. Cancellation and timeout stop waiting, but a synchronous or remote action may already have taken effect. Do not automatically retry a potentially mutating call.

The gateway does not automatically retry an invocation whose outcome is unknown.
Upstream MCP reconnection can refresh a failed session for a later call without replaying the failed action.
Existing tool-specific authorization, provider scopes, worker isolation, and filters remain authoritative.
