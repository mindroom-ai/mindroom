# Frontend Agent Prompt: Cinny Local MindRoom Onboarding

```text
You are working in our `../mindroom-cinny` fork.

Goal:
Implement a smooth “Connect Local MindRoom” onboarding flow for users who are already logged into chat.mindroom.chat via OAuth. This must work with our homeserver stack (`mindroon-tuwunel`), and must not assume Synapse-specific UI behavior.

Product flow:
1. User opens Settings -> “Local MindRoom”.
2. User clicks “Connect Local MindRoom”.
3. Frontend calls provisioning API to start pairing.
4. Frontend shows a one-time pair code and command:
   `uvx mindroom connect --pair-code <CODE>`
5. Frontend polls pairing status until connected.
6. On success, show connected state, linked machine name, created time, last seen.
7. Allow revoke/disconnect from UI.

Important constraints:
- Do not embed homeserver admin secrets in frontend.
- Use existing authenticated browser session/cookie to call provisioning API.
- Keep implementation homeserver-agnostic (works with tuwunel backend APIs exposed through provisioning service).
- UX must be very clear for non-technical users.
- Handle expiration, invalid code, network failures, and already-connected states gracefully.

Assumed API contract (implement client for this):
- POST `/v1/local-mindroom/pair/start`
  - returns: `{ pair_code, expires_at, poll_interval_seconds }`
- GET `/v1/local-mindroom/pair/status?pair_code=...`
  - returns: `{ status: "pending" | "connected" | "expired", connection?: {...} }`
- GET `/v1/local-mindroom/connections`
  - returns: `{ connections: [...] }`
- DELETE `/v1/local-mindroom/connections/{id}`
  - revokes a connection

Implementation requirements:
- Add a dedicated settings panel/page component.
- Add API client methods and typed response models.
- Add countdown timer for pair code expiry.
- Add polling with cleanup on unmount.
- Add loading/empty/error/success UI states.
- Add copy-to-clipboard button for the CLI command.
- Add “Try again” / “Generate new code” action on expiry.
- Add revoke action with confirmation.
- Keep styling consistent with existing Cinny patterns.

Code quality:
- Keep changes focused and small.
- Reuse existing API/auth utilities and design system components.
- Add tests for:
  - successful pairing flow
  - expired code flow
  - polling transition pending -> connected
  - revoke connection flow
  - API error rendering

Deliverables:
1. Summary of files changed.
2. Screenshots or short GIF of the full flow.
3. Test results.
4. Any API contract mismatches discovered.
```
