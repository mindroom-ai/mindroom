# Connections Portal Sign-In from MindRoom Chat

## Goal

People who use a deployment's agents should connect their own OAuth accounts in the Connections portal without dashboard access and without an identity gateway.
Trusted-upstream strict JWT, the only way in before, replaces API-key dashboard auth on every route, which breaks the dashboard login, CLI and MCP clients that use `MINDROOM_API_KEY`, and MindRoom Chat's own calls on a typical self-hosted install.
Everyone who would use the portal is already signed in to MindRoom Chat with a Matrix account on the deployment's homeserver.

## Decisions

### Hand the token over with `postMessage`, never in a URL

Chat opens the portal, the portal asks `window.opener` for a token, and Chat answers only that window at the backend origin.
The portal reports the origin of Chat's message, and the backend accepts only origins in `MINDROOM_CONNECTIONS_ALLOWED_ORIGINS`.
Rejected: a token in the URL fragment, which lands in browser history and lets anyone send a victim a link that signs them in as the attacker (login CSRF), after which the victim may connect their Google account to the attacker's agent.
Rejected: a one-time code minted by a cross-origin request, which keeps the token out of the URL, but an attacker can still mint a code for their own account.

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

Chat requests a bound token by posting `{"io.mindroom.audience": "<backend origin>"}` to `request_token`, and the homeserver advertises `io.mindroom.openid_audience` in `unstable_features` while binding is on.
MindRoom takes the audience only from `MINDROOM_PUBLIC_URL`, never from the request, because a replaying backend controls the `Host` header.
Unbound tokens keep working for Element Call, LiveKit, provisioning pairing, and stock homeservers.
The rollout order is an operator constraint and lives in [Matrix sign-in from MindRoom Chat](../deployment/trusted-upstream-auth.md#matrix-sign-in-from-mindroom-chat).

## Limitations

- Native Chat builds do not offer Connections, because the shells have no pop-up window to hand a token to.
- The MCP Gateway still needs trusted upstream, because SCIM-provisioned MCP accounts are looked up by email, which Matrix sign-in does not provide.
- Chat reaches only the backend set under Computers.
- Without binding, a token stays replayable within its lifetime.
