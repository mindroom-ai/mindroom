# Connections Portal Sign-In from MindRoom Chat

## Goal

People who use a deployment's agents should connect their own OAuth accounts in the Connections portal without dashboard access and without an identity gateway.
Trusted-upstream strict JWT, the only way in before, replaces API-key dashboard auth on every route, which breaks the dashboard login, CLI and MCP clients that use `MINDROOM_API_KEY`, and MindRoom Chat's own calls on a typical self-hosted install.
Everyone who would use the portal is already signed in to MindRoom Chat with a Matrix account on the deployment's homeserver.

## Decisions

### Hand the token over with `postMessage`, never in a URL

Chat opens the portal, the portal asks `window.opener` for a token, and Chat answers only that window at the backend origin.
The portal reports the origin of Chat's message, and the backend accepts only allowed Chat origins (see [the default list](#default-the-allowlist-to-mindrooms-own-chat)).
Rejected: a token in the URL fragment, which lands in browser history and lets anyone send a victim a link that signs them in as the attacker (login CSRF), after which the victim may connect their Google account to the attacker's agent.
Rejected: a one-time code minted by a cross-origin request, which keeps the token out of the URL, but an attacker can still mint a code for their own account.

### Turn the portal on by default, with no named agent

The portal shows only what a user can already reach: the agents they may use and the shared agents whose credentials they manage, and each connect is authorized by its own credential scope rules.
So the opt-in switch protected nothing, and the single named private agent was left over from the first portal, which served one private agent only.
`MINDROOM_CONNECTIONS_AGENT` keeps two jobs: the MCP gateway's first selection, and under trusted upstream auth the dashboard's administrator check.
Rejected: an administrator-only dashboard for every trusted upstream deployment, because header-only gateways may send no Matrix ID, which would lock their operators out.

### Default the allowlist to MindRoom's own Chat

The allowlist exists only against login CSRF, where a page hands the portal an attacker's token.
Unset, it allows MindRoom's hosted Chat, which MindRoom operates, plus any Chat on a local address while the portal itself is on one, so a Docker stack on a home network works without knowing the host's address.
The local rule never applies to a portal on a public address, and `[]` still turns sign-in from Chat off.
The default list also waits for a homeserver that binds tokens: without binding, any backend a user opens from Chat could replay their token to every deployment it can reach, a risk the old opt-in left to operators who set the list.
Rejected: waiving binding for a portal on a local address, because without `MINDROOM_PUBLIC_URL` the address comes from the `Host` header, which a replaying backend sets to `localhost`.

### Allow plain HTTP on local addresses

A local Docker stack serves Matrix and Chat over plain HTTP, so requiring HTTPS for the portal alone protected nothing there; like Chat for Computers URLs, MindRoom accepts it on `localhost`, loopback, RFC 1918, and IPv6 unique-local addresses.
Browsers refuse `Secure` cookies over HTTP, so the session cookie drops the `__Host-` prefix there, and an HTTPS portal ignores that plain name so a sibling host cannot plant a session.

### Sign a process-local cookie instead of storing sessions

The session cookie carries the Matrix ID and expiry, signed with a key generated at process start.
That gives the same fixed one-hour lifetime and the same sign-out on restart as an in-memory store, without memory that needs per-user and global caps.
Rejected: an in-memory store keyed by token digest, the first version, whose caps and eviction existed only to bound its own memory.

### Record which identity started each OAuth flow

A browser can hold both a dashboard login and a portal session, and each authenticates different routes.
The pending OAuth state records whether a portal session started the flow, and the callback authenticates with that identity only.
Without it, the deployment owner holding both could not finish a dashboard OAuth flow for up to an hour after opening the portal.

### Bind tokens to one backend through the Tuwunel fork

A Matrix OpenID token proves the user to anyone holding it, and one public homeserver serves many deployments, so a backend that Chat opens could replay a user's token to another deployment's portal or Computers API.
MindRoom's Tuwunel fork (mindroom-ai/mindroom-tuwunel#105, behind its `mindroom_openid_audience` option) binds a token to an audience:

| Token | Audience asked at `/_matrix/federation/v1/openid/userinfo` | Result |
|---|---|---|
| bound to A | A | 200 |
| bound to A | B or none | 401 |
| unbound | A | 401 |
| unbound | none | 200 |

Chat requests a bound token by posting `{"io.mindroom.audience": "<backend origin>"}` to `request_token`.
MindRoom takes the audience from `MINDROOM_PUBLIC_URL`, never from the request, because a replaying backend controls the `Host` header.
Unset, it uses `http://localhost:<MINDROOM_PORT>`, a fixed value that is the origin Chat opens for a backend on the same machine, so `mindroom run` beside hosted Chat keeps working once its homeserver binds tokens; only Chat on that machine requests tokens for it.
Unbound tokens keep working for Element Call, LiveKit, provisioning pairing, and stock homeservers.
MindRoom sends the audience on every verification instead of first checking the homeserver's `io.mindroom.openid_audience` capability, because the fork ignores it while binding is off and Synapse and upstream Tuwunel ignore unknown userinfo parameters.
Rejected: probing `/versions` and caching the answer, the first version, which needed a retry for a stale cache and guarded only against a homeserver that refuses unknown parameters.
The rollout order is an operator constraint and lives in [Token binding](../connections-portal.md#token-binding).

## Limitations

- Native Chat builds do not offer Connections, because the shells have no pop-up window to hand a token to.
- The MCP Gateway still needs trusted upstream strict JWT; sessions from Chat reach its portal controls only through the email-to-Matrix template mapping, because they carry no email.
- Chat reaches only the backend set under Computers.
- Without binding, a token stays replayable within its lifetime.
