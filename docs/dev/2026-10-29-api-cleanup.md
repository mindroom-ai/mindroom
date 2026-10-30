# API cleanup: one config commit, one request identity

This record holds the decisions behind one write protocol for the config file, one typed request identity with one requester rule, and OAuth operations that never authenticate a request themselves.

## Decisions

- Every config file write MindRoom makes while it runs goes through one commit: the dashboard's full, raw, and entity saves, the config tools, and `!config set`; the one-time access migration takes the same lock.
  The `mindroom config` CLI writers run before MindRoom does and stay outside it.
  The API publishes the committed config to every in-process API state while it holds their locks, so no load or dashboard save can slip between the file write and the publication.
- A change derived from a loaded config (a tool, `!config set`, or a dashboard entity save built from the published config) is refused when the file's fingerprint changed since that load.
  A dashboard full or raw save stays exempt and checks only that nothing was published since its request pinned the snapshot, because it deliberately lets a save repair a broken file no reload has read (`docs/dev/2026-10-28-lifecycle-owners.md`).
- A refused tool change is reported to the agent instead of retried: the change was computed from the old file, so replaying it on the new one could apply a decision made on stale data.
- The commit lock order is the app registry lock, then every registered API state lock (in id order, before filtering by runtime), then the config write lock that personal-agent provisioning shares.
  Locking every state before filtering keeps a state that rebinds onto the runtime mid-commit from missing the publication.
- The one-time access migration's compare-and-replace takes the same config write lock, because it runs on any load that sees the old format, including an old file restored while MindRoom runs.
- Knowledge-mode reconciliation runs at publication, from the exact replaced and new configs, so every writer gets it; the embedded runtime still reconciles the configs it applies, and marking an index stale twice is harmless.
- A commit publishes the config it validated with the personal-agent roster it read; a roster entry settled meanwhile reaches the API views through the reload that provisioning requests and through the runtime's reload of the committed file.
- The request identity is a frozen `AuthUser`; one `requester_id()` rule maps a signed-in Matrix user to themselves and an operator login (standalone or hosted platform) to the configured owner.
  A platform login without `ACCOUNT_ID` accepts any user of its Supabase project; it already acts as the owner for the owner's private credentials and can edit the whole config, so letting it open the owner's reports adds no authority.
- Reports also admit a login through an identity it carries, because a gateway's user-ID claim may itself be a Matrix ID when its Matrix claim is absent.
- OAuth operations never authenticate a request; each router authenticates through its own dependency, so the dashboard route gate no longer exempts `/api/connections` paths.
  The callback, success, and reset exemptions stay, because those handlers validate their own state or reset capability.
- The auth clients are cached once per runtime on the app, resolved once per request, and a request whose app moved to another runtime meanwhile is refused as stale when it rebinds its snapshot, so its auth state and its config never name two runtimes.

## Rejected alternatives

- A registry of "config views" under `config/` that the API registers into: it needs a protocol whose only implementer is the API state, and a callback for the dashboard's pinned-snapshot check, because the publication must hold the API locks across the write.
- Caching the auth state per config generation: it reads only the environment, so rebuilding it per save would drop the Supabase client and JWKS cache for nothing.
- Gating only reports on `ACCOUNT_ID`: that keeps two requester rules; gating credentials too would move private credentials in an unhosted configuration.
