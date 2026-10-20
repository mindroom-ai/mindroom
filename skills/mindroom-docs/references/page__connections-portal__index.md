# Connections Portal

The Connections portal at `/connections` lets each person who uses a deployment's agents connect and disconnect their own OAuth accounts without administrator dashboard access.
It is on by default, and users open it from MindRoom Chat.

## Open It From MindRoom Chat

In MindRoom Chat, the user opens **Connections** in Settings, General, which opens `/connections/` in a new window and signs them in with the account Chat is signed in to.
MindRoom needs no settings for this when Chat is MindRoom's hosted Chat at `https://chat.mindroom.chat`, or when MindRoom and Chat both run on local addresses, as in the Docker stack.
When other devices on a home network reach MindRoom, set `MINDROOM_PUBLIC_URL` to the address Chat opens, such as `http://192.168.1.10:8765`, so OAuth callbacks come back to MindRoom and a homeserver that binds tokens accepts them.
Chat opens the backend set as its Computers backend: the deployment key `mindroom.computers.apiUrl` in Chat's `config.json`, or each user's Computers URL in Settings, General.
Hosted MindRoom Chat ships `mindroom.computers.apiUrl` empty, so its users enter their backend's URL under Computers before the card works.
Native MindRoom Chat apps do not offer Connections yet.

Sign-in works as follows:

1. The portal shows nothing until sign-in succeeds, then shows `Signed in as <Matrix ID>`.
   It ignores any session the browser still holds, so the account signed in to Chat replaces another account's session in the same browser.
2. MindRoom accepts only users of its configured homeserver, and only from the Chat origins it allows (see `MINDROOM_CONNECTIONS_ALLOWED_ORIGINS` below).

When the portal is opened directly rather than from Chat, or reloaded after the Chat tab that opened it closed, it uses the existing session or a [trusted upstream](https://docs.mindroom.chat/deployment/trusted-upstream-auth/) identity, or asks the user to open Connections from Chat.
If two Connections tabs, on the Agents or Agent Keys page, are signed in as different accounts, the older tab refuses further requests and asks the user to reload.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `MINDROOM_CONNECTIONS_ALLOWED_ORIGINS` | unset | JSON list of the exact MindRoom Chat origins that may sign users in, such as `'["https://chat.example.org"]'`; unset or blank allows `https://chat.mindroom.chat`, plus any Chat on a local address while MindRoom itself is on a local address; `[]` turns sign-in from Chat off |
| `MINDROOM_PUBLIC_URL` | unset | The origin users open the portal on, which must be HTTPS or plain HTTP on a local address; unset, the portal accepts the request's own origin, while OAuth callbacks and the token audience use `http://localhost:<port>` |
| `MINDROOM_CONNECTIONS_AGENT` | unset | Optional private agent (`private.per: user` or `user_agent`) that the [MCP Gateway](https://docs.mindroom.chat/deployment/mcp-gateway/) selects for each user at first; under trusted upstream auth it also limits the dashboard to `administrators` (see [Security Boundary](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#security-boundary)) |

A configured allowlist replaces both defaults.
Its entries are HTTPS origins, HTTP origins on a local address, or `capacitor://localhost`, without a path or wildcard, and one invalid entry disables the whole list.
A local address is `localhost` or a loopback or private IP address, such as `http://192.168.1.10:8080`; DNS names need HTTPS even when they resolve to a private address.
Sign-in, connect, and disconnect requests must come from the `MINDROOM_PUBLIC_URL` origin, so Chat's Computers URL must be that origin.
Over plain HTTP, OAuth providers decide which redirect addresses they accept; Google, for example, accepts plain HTTP only for `localhost`.

## What The Portal Shows

The portal groups OAuth services by agent, covering every agent the user may use, private or shared, and the shared agents whose credentials the user may manage.
Services come from each agent's tools, including deferred tools and plugin or MCP OAuth providers; tools that need no browser sign-in are listed too, and room-dependent tools are marked **MindRoom only**.
Each service's status loads independently, so one failing or unconnected service does not block the others.
Using an agent requires a matching `access.users` grant, administrator authority, or membership in a configured grant room; `access.current_room_members` does not apply because browser requests have no current room.
Users listed in `agents.<name>.credential_managers` see that shared agent even without use access, but credential management alone grants no tool access.
Who may manage each connection follows [Who Can Connect An Account](https://docs.mindroom.chat/oauth-framework/#who-can-connect-an-account), and connections made here are the same ones tools use.
Disconnecting a shared connection affects every agent using its [credential scope](https://docs.mindroom.chat/oauth-framework/#where-connections-are-stored).
While the [egress broker](https://docs.mindroom.chat/deployment/egress-broker/) is on, the portal and its Agent Keys page link to each other.
The portal never shows tokens, model configuration, generic credential editing, or OAuth client settings; operators still configure OAuth clients.
When a service uses a shared service account, such as `GOOGLE_SERVICE_ACCOUNT_FILE`, the portal does not show it as a personal connection and disables **Connect** for that service.
Users choose the agents and tools the optional [MCP Gateway](https://docs.mindroom.chat/deployment/mcp-gateway/#choose-exposed-agents-and-tools) exposes on the same page.
Users signed in from Chat have no email, so when MCP accounts are [SCIM-provisioned](https://docs.mindroom.chat/deployment/mcp-gateway/#managed-account-provisioning), that section uses the one active account whose `userName` maps to the signed-in Matrix user through `MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE` and `MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN`, the mapping a trusted upstream sign-in with that email uses.
Without that mapping, when `MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM` is set (a signed Matrix claim takes precedence over the template), or when more than one active account maps to the same Matrix user, it still needs trusted upstream sign-in.

## Sessions And Dashboard Access

The session lasts one hour.
A restart signs everyone out; opening Connections from Chat again signs the user in again.
The portal shell at `/connections` is served without authentication, and every API route stays protected; API requests without a valid sign-in return `401`.
`GET /api/connections/session` returns the signed-in Matrix ID, or `401` without a sign-in.

Apart from the dashboard rule below, a session authenticates only these paths:

- `/connections` and its sub-paths.
- `/api/connections` and its sub-paths.
- The OAuth callback, `/api/oauth/{provider}/callback`, for flows started under the session.
- The OAuth success page, `/api/oauth/{provider}/success`.

For a user listed in `administrators`, the session also opens the dashboard and its APIs, with no API key, when the homeserver binds tokens: MindRoom's Tuwunel fork with `mindroom_openid_audience` on, which MindRoom detects from `/_matrix/client/versions` at sign-in.
Without binding, a token could have been replayed from another deployment on the same homeserver, and the dashboard is full control of this one.
So the session reaches the dashboard only when it was started while the homeserver bound tokens, only while the user is still in `administrators`, never in hosted platform mode, and never to finish an OAuth flow that the API key started.
The Connections header links the dashboard for administrators only.
Everyone else, and every session on a homeserver that does not bind tokens, keeps needing `MINDROOM_API_KEY` for the dashboard, which the CLI, MCP clients, and scripts use as before.
Sign-in from Chat works alongside `MINDROOM_API_KEY` dashboard auth and alongside trusted upstream sign-in.

## Token Binding

MindRoom sends the normalized origin of `MINDROOM_PUBLIC_URL` (lowercase scheme and host, no path, no default port) to the homeserver as the token's audience, or `http://localhost:<port>` when it is unset, the address Chat opens for a backend on the same machine.
A homeserver that binds tokens, MindRoom's Tuwunel fork with `mindroom_openid_audience` on, then accepts only tokens Chat requested for that origin, and other homeservers ignore the audience.
MindRoom never derives the audience from the request, because a replaying backend controls the `Host` header, so on a binding homeserver sign-in fails with 401 until `MINDROOM_PUBLIC_URL` names the origin Chat opens.
Without binding support tokens are not bound, and a backend that a user points Chat at could replay the token to another deployment on the same homeserver until the token expires.
On such a homeserver, keep the OpenID token lifetime short (`openid_token_ttl` on Tuwunel).
Roll out in this order: first MindRoom backends, then MindRoom Chat clients including the iOS app, and only then the homeserver release that binds tokens.
Older backends verify Computers tokens without an audience and would reject Chat's bound tokens with 401.
Older Chat clients, including iOS app versions that are still in App Store review, send unbound tokens that a new backend rejects with 401 once the homeserver binds.
The request-by-request exchange is described in [Connections portal sign-in](https://docs.mindroom.chat/architecture/security-posture/#connections-portal-sign-in).

## Trusted Upstream Sign-In

Behind an identity gateway, users can also reach the portal through [strict JWT mode](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#strict-jwt-mode) with a Matrix identity from a signed Matrix claim or the email-to-Matrix template; header-only, API-key, and `MINDROOM_OWNER_USER_ID` authentication are rejected.
To share a hostname with another frontend, forward `/connections`, `/connections/*`, `/api/connections`, `/api/connections/*`, and `/api/oauth/*` to the MindRoom API, behind the authenticated upstream when you use trusted upstream sign-in.
Exclude `/connections` from the other application's service-worker navigation fallback.
Portal assets are served under `/connections/assets/`, so root `/assets/` can keep serving the other application.

## Troubleshooting

| Status and message | Fix |
| --- | --- |
| `401 Connections sign-in required` | Open Connections from MindRoom Chat, or enable strict JWT mode |
| `403 Connections require a verified Matrix identity` | Enable strict JWT mode with a Matrix claim or email-to-Matrix template |
| `401 Invalid sign-in request` | The sign-in body was malformed, such as an incomplete OpenID token from Chat; open Connections from MindRoom Chat again, and update MindRoom Chat and MindRoom if it keeps failing |
| `403 Connections sign-in is not allowed from this client` | Add the exact Chat origin to `MINDROOM_CONNECTIONS_ALLOWED_ORIGINS`, or remove an `[]` value; fix any invalid entry because one disables the whole list |
| `401 Matrix OpenID verification failed.`, `Invalid Matrix OpenID response.`, `Invalid Matrix OpenID subject.`, or `OpenID server does not match the configured Matrix server.` | Sign in to MindRoom Chat on the same homeserver as MindRoom, then open Connections again; on a homeserver that binds tokens, also set `MINDROOM_PUBLIC_URL` to the origin Chat opens |
| An administrator's Dashboard link from Connections asks for the API key | Check that `/_matrix/client/versions` lists `io.mindroom.openid_audience` as `true` and `MINDROOM_PUBLIC_URL`, or `http://localhost:<port>` when unset, is the origin Chat opens; the warning `Could not check whether the homeserver binds OpenID tokens` means MindRoom could not read `/versions`, and `Dashboard access check failed after the binding probe` means the second token check failed. Open Connections from Chat again afterwards, because the session records dashboard access at sign-in |
| `503 Matrix OpenID verifier is unavailable.` | MindRoom could not reach the configured homeserver; check that it is up and reachable from MindRoom |
| `403 Connections require a configured private agent` | Point `MINDROOM_CONNECTIONS_AGENT` at an agent with `private.per: user` or `user_agent`, or unset it |
| `403 No connections are available for this account` | Give the user agent access or list them in `credential_managers` |
| `403 Connections require an HTTPS public origin, or HTTP on a local address` or `Connection changes require a same-origin request` | Use HTTPS, or a local address for plain HTTP, and open the portal from the `MINDROOM_PUBLIC_URL` origin |
| `404 Agent Keys are not enabled` | Turn the [egress broker](https://docs.mindroom.chat/deployment/egress-broker/) on |
