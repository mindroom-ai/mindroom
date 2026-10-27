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
- **A service declares whether read-only means anything for it.** HTTP methods map onto reads and writes only for APIs that follow REST conventions, so the method alone is not the rule.
  `EgressService` gains `read_only: EgressReadOnly | None`, where `EgressReadOnly` has `post_paths: list[str]` (segment prefixes, matched like `path_prefix`) naming the endpoints that read with `POST`.
  `None` (the default) means read-only is not offered for that service: the APIs refuse `"read"` for it and the dashboard does not show the choice.
  The Google presets (`google_drive`, `google_gmail`, `google_calendar`, `google_sheets`, `google_docs`, `google_tasks`) and `atlassian` declare it; `google_calendar` lists `/calendar/v3/freeBusy`.
  `github` does not yet: git clone and fetch read with `POST …/git-upload-pack`, which a path prefix cannot name.
  Other services, including a deployment's own, opt in by setting the field.
- **The broker enforces it before using the connection.** `mitm.py` passes the request's method, whether it is a WebSocket upgrade, and whether it carries a method-override header (`X-HTTP-Method-Override`, `X-HTTP-Method`, `X-Method-Override`) to the secret resolver, and `resolve_oauth_token` checks the level right after the opt-in check, before reading or refreshing the connection.
  Under `"read"`, a request gets the token only for `GET` and `HEAD`, or for `POST` to one of the service's `post_paths`; anything else, any WebSocket upgrade, and any method-override header is refused with 403 and refusal code `read_only`, audited like the other refusals.
  Batch endpoints and GraphQL mix reads and writes in one `POST`, so they stay refused under `"read"` unless a service lists them.
  The worker never holds the token, so it cannot route around the check.
- **Only connected-account opt-ins get a level.** A stored API key is scoped where it is issued (a fine-grained token, a read-only key), so the key path stays as it is.
- **The APIs take the level when allowing an account.** `OAuthReuseRequest` gains `access: Literal["read", "read_write"] = "read_write"`, used by both opt-in endpoints (the personal Agent Keys page and the administrator's services page); `"read"` for a service without `read_only` is a 409.
  The opt-in's status reports the level and whether the service offers read-only.
  The API default keeps existing clients unchanged; the dashboard chooses explicitly.
- **The dashboard asks, and defaults to read-only where offered.** Allowing an account on a service row that offers read-only shows "Read only" (preselected) and "Read and write"; an allowed row shows its level and lets the user change it.
  Changing the level rewrites the one entry; turning the opt-in off still removes it.
- **The agent tool reports the level.** `list_egress_credentials` adds `oauth_access` next to `oauth_reuse`, and the `read_only` refusal tells the agent to send the user the Agent Keys page to allow writes, mirroring the existing `oauth_reuse: "off"` refusal.

## Later, not now

- **Several accounts per provider** is designed separately (`2026-10-27-agent-keys-multiple-accounts-design.md`); its opt-in entries carry the same `access` field.
- **Write-only access** (for example sending mail without reading it). The same check could allow only non-read methods, but no routine needs it yet.
- **Read-only for `github`**, which needs a way to name `…/git-upload-pack` paths.

## Assumptions

- The APIs behind the declared presets do their reads with `GET` (or `HEAD`), apart from the `post_paths` they list.
- Squid hands the broker every credentialed request, so the broker sees each request's method and headers before any credential is added.

## Testing

- Config tests: `read_only` defaults to `None`, validates `post_paths` like `path_prefix`, and the declared presets carry it.
- Broker tests: an entry without `access` reads as `"read_write"`; under `"read"`, `GET` and `HEAD` pass, `POST` passes only on a listed path, and `POST` elsewhere, `PUT`, `PATCH`, `DELETE`, a WebSocket upgrade and a method-override header are refused with `read_only` without resolving the token, and the refusal is audited.
- API tests: both opt-in endpoints store the requested level, default to `"read_write"`, refuse `"read"` with 409 for a service without `read_only`, report the level, and keep every other entry when the level changes.
- Tool and dashboard tests: the level shows in `list_egress_credentials` and on the service row, and the dashboard sends `"read"` by default where read-only is offered.
