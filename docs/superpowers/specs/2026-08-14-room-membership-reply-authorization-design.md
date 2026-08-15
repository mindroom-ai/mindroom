# Room Membership Reply Authorization Design

## Goal

Allow current membership in configured managed Matrix rooms to grant conversation access to an agent or team anywhere that entity is already available.

Keep room access, responder availability, and credential or OAuth management as independent security boundaries.

## Configuration

Each `authorization.agent_reply_permissions` value accepts either the existing list shorthand or a structured policy.

```yaml
authorization:
  agent_reply_permissions:
    example_agent:
      users:
        - "@operator:example.com"
      joined_rooms:
        - example-agent-room
```

The existing list form remains valid and has the same behavior as a structured policy containing only `users`.

```yaml
authorization:
  agent_reply_permissions:
    example_agent:
      - "@alice:example.com"
```

Entity keys continue to accept configured agents, configured teams, `router`, and `*` only.

An explicit entity policy continues to override `*`.

If neither an explicit policy nor `*` exists, replies remain unrestricted by entity reply policy.

Structured `users` entries retain exact, glob, and `*` matching after bridge-alias resolution.

Structured `joined_rooms` entries must be unique managed room keys present in `Config.get_all_configured_rooms()`.

Room IDs and aliases are not accepted as `joined_rooms` values because authorization must resolve a managed key through persisted managed-room state to a stable room ID.

## Authorization Semantics

The central reply evaluator selects the explicit entity policy or the wildcard fallback and resolves the sender through `authorization.aliases` before evaluating it.

Reply access is granted when the resolved sender matches a static `users` entry or is a current joined member of any configured `joined_rooms` room.

Only `membership == "join"` grants access.

Invite, leave, kick, and ban states do not grant access.

Current internal MindRoom identities retain their existing bypass, while arbitrary bot-like Matrix IDs do not gain one.

The existing `authorization.room_permissions` check remains the outer room gate.

The existing responder candidate logic remains the availability gate, so authorization never makes an entity available in an unconfigured or unjoined target room.

All reply surfaces continue to use the central evaluator, including text, voice transcription, calls, reactions and edit regeneration, visible router voice echoes, commands, schedules, delegated runtime tools, and external triggers.

Credential and OAuth management evaluates only the selected policy's static `users` entries.

Membership-only policies therefore grant no credential-management access.

## Membership State

Add one orchestrator-owned in-memory membership service shared by all bot and API reply evaluators.

The service publishes immutable snapshots so a message observes either the previous complete snapshot or the replacement complete snapshot.

Each snapshot records the authorization policy signature, the stable Matrix room ID for every referenced managed room key, whether that room is ready, and its canonicalized joined-user IDs.

The policy signature covers configured membership grants and bridge aliases so stale snapshots cannot authorize after a config change.

The router Matrix client is the control-plane client because the router is configured to join every managed room.

An authoritative refresh first resolves every referenced key through persisted `MatrixState.rooms`, confirms the router is currently joined, and then calls `joined_members()` once for each resolved grant room.

Successful member snapshots canonicalize every Matrix user ID through bridge aliases and store only joined users.

An unresolved room, an unjoined router client, or an unsuccessful `joined_members()` result publishes that room as unready and logs a structured warning without logging its member list.

One unready room fails closed independently while another ready room in the same any-of policy may still grant access.

The service does not persist members and performs no Matrix request during a message authorization check.

## Lifecycle

Startup builds the authoritative membership snapshot after managed rooms and router membership have been reconciled and before runtime readiness exposes membership grants.

Every live actionable `m.room.member` event already admitted through the router's durable room-lifecycle stream updates the active snapshot, regardless of whether the transition is a join, invite, self-leave, kick, or ban.

Ordinary user transitions take effect in accepted durable LIVE-event order rather than being preapplied from an unaccepted raw sync response.

The runtime does not promise total ordering between the router membership stream and independently syncing agent-room streams.

The existing `room:member_left` hook remains unchanged because it intentionally represents human self-leaves rather than the full authorization transition stream.

Starting a replacement router receive loop invalidates membership readiness until an authoritative refresh succeeds.

The first successful router sync after a receive-loop start refreshes the configured grant rooms.

A limited timeline, unrecovered room, rejected checkpoint, or other uncertain router sync condition invalidates and refreshes affected membership state authoritatively.
Accepted and ordered limited-timeline state invalidates grants before nio admits any timeline event from that response.

Configuration reload runs behind the existing response-admission gate.
Authorization-sensitive turn planning retains an admission slot through response-runner handoff so a reload cannot commit between an allow decision and its response.

It reconciles any new managed rooms, builds a snapshot for the new policy, atomically replaces the service snapshot, and only then re-exposes runtime delivery.

A policy-signature mismatch fails closed during any intermediate config-publication window, preventing an old room grant from authorizing under a new policy.

Shutdown clears membership readiness so a stopped control plane cannot retain authorization power.

## Components

`src/mindroom/config/auth.py` defines the normalized structured policy and validates duplicate room references.

`src/mindroom/config/main.py` validates entity keys and managed room-key references.

`src/mindroom/agent_reply_membership.py` owns immutable membership snapshots, authoritative refresh, transition application, invalidation, and pure membership lookup.

`src/mindroom/authorization.py` remains the single policy evaluator and separates reply evaluation from static-only credential evaluation.

`src/mindroom/orchestrator.py` owns the service and coordinates startup, reload, and shutdown refreshes.

`src/mindroom/bot.py` forwards router sync certainty and durable room-member transitions to the orchestrator-owned service.

Typed runtime dependencies expose the same service to bot collaborators, MatrixRTC calls, command and scheduling paths, tool-time candidate resolution, and the external-trigger API.

## Logging and Failure Handling

Warnings identify the entity policy when applicable, managed room key, stable room ID when known, readiness result, and failure reason.

Transition logs identify the room key and room ID, transition membership, and authorization source without including complete membership lists.

Failed refreshes do not reuse a previously ready membership snapshot for the affected room.

Static users and internal identities remain evaluable when a membership room is unready.

## Testing

Configuration tests cover legacy list parsing, structured parsing, entity validation, duplicate room rejection, unknown or non-key room rejection, and serialization used by config editing surfaces.

Pure authorization tests cover legacy and structured users, wildcard precedence, user globs, `*`, internal bypass, arbitrary bot-like IDs, alias resolution, users-or-membership semantics, any-of rooms, and fail-closed readiness.

Credential tests prove membership is never consulted and membership-only policy grants no credential-management access.

Membership-service tests cover authoritative startup snapshots, stable persisted room-ID resolution, router join verification, joined-members failures, canonicalized aliases, join transitions, invite denial, and leave, kick, and ban revocation.

Lifecycle tests cover startup, receive-loop restart, limited or uncertain sync recovery, config reload replacement, and shutdown invalidation.

Call-site tests prove current-room authorization and responder availability remain independent and that each ingress or trigger surface supplies the shared membership service to the central evaluator.

Focused tests run before the repository-wide test, type, lint, format, pre-commit, and diff checks.

## Alternatives Considered

Using each bot's incidental nio room cache would couple authorization to transport cache completeness and would not provide one atomic control-plane view.

Calling `joined_members()` during each message would provide fresh state but violate latency, availability, and no-per-message-I/O requirements.

Copying joined users into config or another durable allowlist would create a second source of truth and weaken immediate revocation.

The orchestrator-owned immutable snapshot is the smallest design that preserves centralized policy, fail-closed recovery, hot reload, and Matrix as the sole membership authority.
