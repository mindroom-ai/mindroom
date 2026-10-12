# MindRoom Security Review: Authentication & Authorization

September 2025 review of SaaS platform backend authentication, admin authorization, and session and token handling (the scope of reviews 01 and 05).
Findings that the current code fixes are omitted; what remains are the assumptions and decisions behind the design and the items still open.

## Assumptions

- Supabase Auth owns the token lifecycle: it issues JWTs, manages and rotates the signing keys, refreshes sessions in the browser SDK, and checks signature and algorithm when the backend calls `get_user`, so `alg: none` and algorithm-confusion attacks are Supabase's to reject.
- The backend never verifies a Supabase user JWT's signature itself and never sees token refreshes, so it has no visibility into refresh failures or refresh-token reuse.

## Decisions

- Token guessing needs no lockout, because Supabase signs its tokens; the per-IP lockout counts only malformed authorization headers.
  Supabase token rejections do not count, because Supabase also rejects authentic tokens whose session ended or that just expired, so counting them would lock out legitimate clients.
  Padding every auth check to a fixed minimum duration was rejected as overengineered for a theoretical attack.
- The remaining timing differences, such as a cached versus an uncached token check, could hint whether a token is valid; this is accepted as low risk with little practical exploitation.

## Open

- A token whose Supabase session ends, for example by sign-out, keeps passing backend auth for up to 5 minutes while it is in the backend's auth cache, because nothing propagates Supabase-side revocation to the cache; an uncached check rejects it, since Supabase's user lookup requires the session to still exist.
- Admin accounts have no MFA or step-up authentication before sensitive actions.
