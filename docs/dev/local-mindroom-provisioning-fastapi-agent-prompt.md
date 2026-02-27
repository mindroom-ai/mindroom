# Backend Agent Prompt: FastAPI Local MindRoom Provisioning Service

```text
You are implementing a hosted FastAPI provisioning service for MindRoom onboarding.

Context:
- Users log into `chat.mindroom.chat` via OAuth.
- They run MindRoom backend locally (`uvx mindroom ...`).
- Homeserver stack is `mindroon-tuwunel` (not Synapse-specific assumptions).
- Humans remain OAuth-only; agents must be provisioned securely by backend automation.

Goal:
Implement a secure pairing and provisioning backend that enables this UX:
1. User clicks "Connect Local MindRoom" in chat UI.
2. UI receives a short-lived pair code.
3. User runs `uvx mindroom connect --pair-code <CODE>` locally.
4. Local backend is linked to that OAuth user account.
5. Local backend can request short-lived bot registration credentials when creating agents.

Core requirements:
- Keep public onboarding friction very low.
- No public static registration token.
- Per-user controls, revocation, and auditability.
- Keep provisioning path compatible with `mindroon-tuwunel`.

Implement API (v1):
1) POST `/v1/local-mindroom/pair/start`
   - Auth: required (browser session/cookie/JWT)
   - Returns: `{ pair_code, expires_at, poll_interval_seconds }`
   - Pair code must be short-lived and one-time.

2) GET `/v1/local-mindroom/pair/status?pair_code=...`
   - Auth: required for browser path
   - Returns:
     - pending: `{ status: "pending", expires_at }`
     - connected: `{ status: "connected", connection: {...} }`
     - expired: `{ status: "expired" }`

3) POST `/v1/local-mindroom/pair/complete`
   - Auth: local client credential path (not browser session)
   - Input: `{ pair_code, client_name, client_pubkey_or_fingerprint }`
   - Completes the link and returns local auth material (short-lived access + refresh, or signed client secret flow).

4) GET `/v1/local-mindroom/connections`
   - Auth: required (browser user)
   - Returns linked local installations for that user.

5) DELETE `/v1/local-mindroom/connections/{id}`
   - Auth: required (browser user)
   - Revokes the local installation and all active provisioning credentials.

6) POST `/v1/local-mindroom/tokens/issue`
   - Auth: linked local client auth
   - Input: `{ purpose: "register_agent", agent_hint }`
   - Returns short-lived, limited-use registration credential for homeserver registration flow.
   - This endpoint is what local MindRoom calls before creating new Matrix agent users.

Homeserver integration (`mindroon-tuwunel`):
- Implement an adapter layer, e.g.:
  - `HomeserverProvisioner.issue_registration_token(...)`
  - `HomeserverProvisioner.create_bot_account_direct(...)` (fallback if token flow unavailable)
- Prefer token-based flow if tuwunel supports `m.login.registration_token`.
- Never expose homeserver admin credentials to browser clients.

Security requirements:
- Pair codes: high entropy, short TTL (e.g. 10 min), one-time use.
- Registration credentials: very short TTL, limited uses (ideally 1), scope-limited.
- Rate limits on pair creation, completion, and token issuance.
- Full audit log: who created pair, when linked, when tokens were issued, from which connection.
- Server-side revocation checks on every privileged call.
- Secret redaction in logs.

Data model (minimum):
- `pair_sessions`: id, user_id, pair_code_hash, expires_at, status, created_at, completed_at.
- `local_connections`: id, user_id, client_name, fingerprint, created_at, last_seen_at, revoked_at.
- `issued_credentials`: id, connection_id, type, token_hash, scope, uses_remaining, expires_at, revoked_at, created_at.
- `audit_events`: id, actor_type, actor_id, action, metadata_json, created_at.

Operational requirements:
- Idempotent endpoints where possible.
- Clear error payloads (expired code, invalid code, revoked connection, rate limit).
- OpenAPI docs for all endpoints.
- Health/readiness endpoints.
- Environment-based config for TTLs/rate limits.

Testing requirements:
- Unit tests for pair code lifecycle.
- Unit tests for connection creation/revocation.
- Unit tests for token issuance policy (TTL, usage caps, revocation).
- Integration tests for full flow:
  - pair start -> complete -> connected
  - issue registration token -> consume once -> reject reuse
  - revoked connection cannot issue new tokens
- Tests for tuwunel adapter error handling and fallback behavior.

Deliverables:
1. Summary of architecture and files changed.
2. Final API contract and example request/response payloads.
3. Migration/schema changes.
4. Test output.
5. Explicit list of security controls implemented.

Non-goals:
- Building frontend UI in this task.
- Replacing homeserver auth policy for humans.
```
