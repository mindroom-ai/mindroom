# Read-Only Access for Agent Keys Account Opt-Ins

This record explains why an Agent Keys opt-in for a connected account carries an access level, how the broker enforces it, and what is deliberately left for later.

## Why

An Agent Keys grant should be exactly what the user approved: one credential, sent only to the service's hosts and paths, for the actions the user allowed.
Agent Keys already limits where a credential goes (service rules) and which credential it is (a stored key, or the scope's connection once someone opts in).
It cannot limit what a connected account may do: an opt-in hands the agent's shell and python the connection's full access, writes included.

That leaves two choices where users need a third.
A user whose routine only reads, such as a daily brief built from a calendar, either grants writes it never needed or gets nothing.
Moving grants from Agent Vault, whose logins often had read-only Google scopes, forced exactly that choice: the migration rounded those grants up to read and write, and a later cleanup rounded them down to nothing, breaking routines that only read.

## Decisions

- **The opt-in entry records an access level.** Each entry in `egress__oauth_reuse` gains `"access": "read" | "read_write"`.
  An entry without the field reads as `"read_write"`, so every existing opt-in keeps working as it does today; nothing is migrated.
  The level is per service, like the opt-in itself, so one account can be read-only for Calendar and read and write for Gmail.
- **The broker enforces it, right before injecting the credential.** `mitm.py` passes the request's method (and whether it is a WebSocket upgrade) to the secret resolver, and `resolve_oauth_token` checks the level right after the opt-in check, before reading or refreshing the connection.
  A request to a service whose opt-in is `"read"` gets the token only for `GET` and `HEAD`; any other method, and any WebSocket upgrade, is refused with 403 and refusal code `read_only`, audited like the other refusals.
  The worker never holds the token, so it cannot route around the check.
  Reads that some APIs only offer over `POST` (Google batch endpoints, GraphQL, Calendar free/busy queries) are refused in read-only mode; each has a `GET` alternative for the common case, and a read-only allowlist of `POST` paths can be added later if a real routine needs one.
- **Only connected-account opt-ins get a level.** A stored API key is scoped where it is issued (a fine-grained token, a read-only key), so the key path stays as it is.
- **The APIs take the level when allowing an account.** `OAuthReuseRequest` gains `access: Literal["read", "read_write"] = "read_write"`, used by both opt-in endpoints (the personal Agent Keys page and the administrator's services page), and the opt-in's status reports it.
  The API default keeps existing clients unchanged; the dashboard chooses explicitly.
- **The dashboard asks, and defaults to read-only.** Allowing an account on a service row offers "Read only" (preselected) and "Read and write", and an allowed row shows its level and lets the user change it.
  Changing the level rewrites the one entry; turning the opt-in off still removes it.
- **The agent tool reports the level.** `list_egress_credentials` adds `oauth_access` next to `oauth_reuse`, and the `read_only` refusal tells the agent to send the user the Agent Keys page to allow writes, mirroring the existing `oauth_reuse: "off"` refusal.

## Later, not now

- **Several credentials per provider.** Agent Vault let a vault hold several logins, with any account and any scopes, separate from the logins the tools use.
  Agent Keys reuses the one tool connection per scope and provider.
  When that is needed, an opt-in entry gains a `credential` reference next to `access`, defaulting to the scope's tool connection; this design leaves room for that without needing it.
- **Write-only access** (for example sending mail without reading it). The same check could allow only non-read methods, but no routine needs it yet.

## Assumptions

- The APIs behind the Google, GitHub and Atlassian presets do their reads with `GET` (or `HEAD`), apart from the batch, GraphQL and query endpoints named above.
- Squid hands the broker every credentialed request, so the broker sees each request's method before any credential is added.

## Testing

- Rule tests: an entry without `access` reads as `"read_write"`; `"read"` allows `GET` and `HEAD` and refuses `POST`, `PUT`, `PATCH`, `DELETE` and WebSocket upgrades with `read_only`.
- Broker tests: a refused write never resolves the token, and the refusal is audited with its code.
- API tests: both opt-in endpoints store the requested level, default to `"read_write"` when it is absent, and report it; changing the level keeps every other entry in the document.
- Tool and dashboard tests: the level shows in `list_egress_credentials` and on the service row, and the dashboard sends `"read"` unless the user picks otherwise.
