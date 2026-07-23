# Origin-room report threat model

Origin-room reports treat the exact Matrix room recorded from trusted tool runtime context as the authorization boundary.

## Prevented risks

- A leaked or guessed report slug grants no access without an authenticated browser principal mapped to a verified Matrix user ID.
- Membership in another room containing the same publisher agent grants no access because both viewer and publisher membership are checked against the recorded origin room.
- Forged query parameters, model tool arguments, and arbitrary identity headers cannot select the viewer, room, or publisher identities used for authorization.
- Unverified email values cannot establish Matrix identity; email mapping is accepted only from the existing trusted authentication integration.
- Direct root-document and nested-asset requests pass through the same authentication, policy, revocation, and current-membership checks.
- Safe artifact resolution, CSP, sandbox, content-type handling, and no-store response headers continue to protect against path traversal and untrusted report content.
- Public and origin-room routes require matching stored policies, so changing route shape cannot reinterpret one policy as the other.
- Malformed protected metadata, publisher identity changes, missing runtime bindings, and Matrix lookup failures fail closed.
- Structured authorization logs omit report slugs, authentication material, report contents, and full Matrix identifiers.

## Bounded risks

- Viewer or publisher departure may remain authorized until a successful membership decision expires from the short bounded cache.
- Concurrent static assets may reuse one successful authorization decision to avoid a Matrix request per asset.
- Revocation is checked from persistent metadata before membership-cache use on every request and therefore takes effect immediately.

## Out of scope

- Core MindRoom does not add a browser login flow, identity-provider integration, dashboard requirement, API-key requirement, or Matrix-client session reuse.
- Deployments must provide an existing trusted browser authentication integration that yields or securely maps to a verified Matrix user ID.
- Room authorization limits report retrieval but does not make generated HTML trusted, so report content remains sandboxed and isolated from privileged application state.
- Origin-room authorization does not prevent an authorized viewer from copying content after retrieval.
