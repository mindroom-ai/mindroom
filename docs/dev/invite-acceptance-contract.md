# Invite Acceptance Contract

## Status

This document defines the authoritative product and engineering contract for accepting Matrix room invitations.
Implementation, tests, documentation, and review findings must be evaluated against this contract.
Changing an accepted failure into a required guarantee requires an explicit contract change before implementation.
This is the target contract for the minimal replacement implementation now in progress.

## Goal

Allow the router to accept an invitation to a newly created room when the exact authenticated inviter is authorized.
Support the private Mind invitation flow without weakening ordinary authorization for agents, teams, messages, calls, reactions, tools, or background work.
Prefer a recoverable need to reinvite over transaction-style recovery machinery.

## Product scenario

1. A user creates a new Matrix room.
2. The user invites the MindRoom router account.
3. The router has `accept_invites: true`.
4. The router normally uses `current_room_members`, but it cannot be a current member before joining.
5. The exact authenticated sender of the fresh router invitation may satisfy `current_room_members` for that invitation only.
6. After the router joins, ordinary room authorization applies to all activity.
7. Agents and teams do not receive this bootstrap exception and must pass ordinary authorization.

## Required guarantees

### Authorization

1. `accept_invites: false` is absolute and prevents invite acceptance.
2. Only a fresh authenticated Matrix self-invite callback may create bootstrap evidence.
3. Saved room IDs, durable pending records, nio cache entries without a matching live callback, stale callbacks, prior invitations, and accepted-room persistence cannot provide bootstrap authorization.
4. Ordinary responder authorization is evaluated before the bootstrap exception.
5. Only the router may bootstrap `current_room_members` from the exact live inviter.
6. Agents and teams require an administrator, static user, internal identity, grant-room authorization, or an already-authoritative router membership grant.
7. The current configuration is read again after a successful join.
8. If acceptance depended on the router bootstrap exception, the inviter must still be a joined room member after the join.
9. A policy revocation that becomes active while `join` is running prevents accepted-room persistence.
10. Every event after joining uses the ordinary post-join authorization policy.

### Join and persistence

1. One live invite token owns at most one join attempt.
2. A join is attempted only while that exact process-local token remains current.
3. A normal joined sync may remove nio's invite-cache entry without revoking the in-flight token.
4. A later authenticated invite callback replaces the previous process-local token for that room.
5. Accepted-room persistence is written only after Matrix confirms the join, post-join setup succeeds, and final authorization passes.
6. Accepted-room persistence preserves an existing joined ad-hoc room across restart.
7. Accepted-room persistence never authorizes joining an absent room.
8. Existing durable pending records may continue to retry invitations that pass ordinary authorization.
9. A durable pending record never supplies or reconstructs the router bootstrap exception.

### Rejection and cleanup

1. This feature never calls Matrix leave as compensation for a rejected or interrupted invitation.
2. A joined room that fails final validation is not persisted as accepted.
3. Existing room cleanup remains the sole owner of later leave decisions and is unchanged by this feature.
4. This feature does not add configuration-snapshot coordination, leave fencing, departure barriers, or cleanup recovery.

## Explicitly accepted failures

The following outcomes are product decisions and are not defects:

- A process stop before accepted-room persistence may require the user to invite the router again.
- Cancellation during join or post-join setup may require another invitation.
- A temporary join failure may require another invitation.
- A joined room that fails final validation may remain joined until existing cleanup or restart processing handles the room.
- Welcome delivery retains its existing behavior, but this feature adds no welcome retry guarantee.
- A crash after Matrix joins but before local persistence is not automatically recovered.
- The router bootstrap exception is not persisted, reconstructed, or retried after restart.
- Existing durable pending work may still retry when ordinary authorization succeeds.
- Rare overlapping departure or cancellation ordering is allowed to converge through a later sync, cleanup, restart, or reinvitation.
- The router's presence in a client-side invite picker is outside this feature.
- Existing multi-room cleanup failure handling is outside this feature and receives no new guarantee.
- Existing decrypt-fence recovery is reused without adding new durable recovery guarantees.

A router that remains joined after an accepted failure still applies ordinary post-join authorization to every event.
An accepted failure must not create an authorization bypass or accepted-room persistence.

## Ownership model

### Matrix and nio

Matrix is authoritative for whether the account is invited, joined, or departed.
Nio owns its live room and invite caches.
The implementation may inspect those caches but must not treat object identity as an immutable invite generation.

### Process-local invite token

One small immutable process-local token identifies the room, authenticated inviter, and callback generation.
The token exists only for one running process.
The token is not durable recovery state.
A later invite callback for the same room replaces it.

### Bot room lifecycle

`BotRoomLifecycle` owns the per-room lock, the single live-bootstrap join attempt, final validation, and accepted-room persistence.
It reuses existing ordinary pending-invite recovery without expanding its authority or guarantees.
It does not own compensating leave, new background retry, or a durable transaction state machine.

### Existing pending-invite store

The existing pending-invite store remains baseline behavior for ordinary invite recovery.
Its records identify work to reconsider, not authorization evidence.
A recovered record must pass ordinary authorization and cannot use the router bootstrap exception.
This feature adds no pending schema, status, generation, revision, migration, or retry policy.

### Accepted-room store

The accepted-room store contains only room IDs whose joins completed and passed final authorization.
It is preservation evidence, not invite evidence, membership evidence, or pending work.
There are no persisted `observed`, `authorized`, `leaving`, generation, revision, or retry states.

## Intended flow

1. Receive an authenticated self-invite callback.
2. Create or select the current process-local invite token for that room.
3. Acquire the existing per-room lifecycle lock.
4. Confirm `accept_invites`, current token ownership, and pre-join authorization.
5. Attempt Matrix join exactly once.
6. On success, read the latest bot configuration.
7. When bootstrap authorization was required, fetch authoritative joined members and confirm the inviter is still joined.
8. Confirm no newer invite token replaced this attempt.
9. Run normal joined-room setup.
10. Persist the accepted room.
11. Send the welcome message as best effort.
12. If final acceptance fails after joining, do not persist acceptance and do not call Matrix leave.
13. Release only the process-local token and pending record owned by this attempt, without deleting evidence for a newer invite.

## Complexity guardrails

- Do not add a durable invite transaction state machine.
- Do not expand the schema or authority of the existing pending-invite store.
- Do not add invite recovery workers, retry loops, retry scheduling, or startup replay.
- Do not add durable invite generations, revisions, invalidation records, or migration code.
- Reuse the existing per-room lock.
- Add at most one small process-local invite-token mechanism.
- Keep `bot.py` limited to lifecycle wiring and calls into the focused lifecycle owner.
- Restrict production changes to `authorization.py`, `bot_room_lifecycle.py`, and `bot.py`.
- Do not modify `orchestrator.py`, configured-room publication, room cleanup, sync continuity, decrypt fencing, or persistence schemas for this feature.
- Keep the runtime change small enough to explain as one callback token flowing through one existing room lock and join owner.
- Treat source size and churn as diagnostics, not acceptance thresholds.
- Stop and reconsider the design if implementation needs another owner, durable state, retry path, cleanup branch, or production file.
- Tests must verify required guarantees and must not assert automatic recovery for accepted failures.
- Documentation must not promise behavior listed under accepted failures.

## Required regression tests

1. The exact router inviter can bootstrap `current_room_members` in a new room.
2. A different or stale inviter cannot use the bootstrap exception.
3. `accept_invites: false` prevents joining.
4. An agent or team without ordinary authorization cannot bootstrap from its inviter.
5. An inviter who is no longer joined after the router joins cannot cause persistence.
6. A config revocation during join prevents persistence.
7. Normal sync removal of the invite cache after join does not revoke the in-flight token.
8. A newer invite callback replaces the older token without relying on nio object identity.
9. Accepted persistence occurs only after successful join, setup, and final authorization.
10. A recovered durable pending invite may retry with ordinary authorization but cannot bootstrap `current_room_members`.
11. Final rejection does not persist acceptance and does not call Matrix leave.
12. Post-join messages continue through ordinary authorization.

Tests for new automatic retry, bootstrap restart recovery, welcome retry guarantees, new durable pending states, repeated cancellation recovery, and exact convergence of accepted failure modes are prohibited unless this contract is explicitly changed.
Existing baseline tests for ordinary pending-invite recovery remain valid.

## Review contract

A review finding is blocking only when it demonstrates one of the following:

- A required guarantee above is violated.
- An authorization bypass is possible.
- The normal successful invitation path fails.
- An unauthorized room becomes durably accepted.
- This feature directly initiates a compensating leave.
- Accepted-room persistence is written before successful final validation.
- The implementation violates the structural complexity guardrails.

A finding that demonstrates only an explicitly accepted failure is non-blocking by design.
A reviewer may propose strengthening the contract, but the implementation must not be expanded until that product change is explicitly accepted.
Every blocking finding must cite the violated contract section.
Review must not infer transaction-like recovery guarantees from general expectations of robustness.

## Definition of done

The private router invitation scenario works in a newly created room.
All required regression tests pass.
The full repository test suite and pre-commit hooks pass.
The runtime diff contains only the scoped owners and has one explainable end-to-end flow.
Two fresh independent full reviews approve the same exact head against this contract.
The implementation adds no durable unfinished-invite machinery and grants no new authority to existing pending records.
All existing invite documentation is aligned with this contract and no longer promises an accepted failure as a guarantee.
