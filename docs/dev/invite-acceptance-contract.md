# Invite Acceptance Contract

## Status

This living document defines the product contract for inbound Matrix invitations while PR 1931 is developed and reviewed.
The implementation, tests, documentation, and review findings must be evaluated against this contract.
This document must be deleted in the final commit before merge.

## Goal

Allow people to create ad-hoc Matrix rooms and invite the router, shared agents, and requester-private agents.
Keep room membership separate from permission to interact with a responder.
Prefer a recoverable need to reinvite over transaction-style lifecycle machinery.

## Configuration

`accept_invites` owns inbound invitation policy for the router and agents.
It accepts three forms.

```yaml
accept_invites: false
```

`false` rejects every inbound invitation.

```yaml
accept_invites: true
```

`true` accepts every valid inbound invitation.

```yaml
accept_invites:
  - "@owner:example.com"
  - "@*:trusted.example.com"
```

A list accepts only inviters whose canonical Matrix user ID matches at least one case-sensitive pattern.
Identity aliases are resolved before matching, consistently with responder `access.users`.
An empty list accepts no inviters.

Teams do not currently expose `accept_invites`, so changing team invitation policy is outside this PR.

## Separation from responder access

Accepting an invitation grants room membership only.
It does not grant permission to send messages, invoke an agent, use tools, approve actions, place calls, or access requester-private state.
Every interaction after joining continues through ordinary responder authorization.

`current_room_members` means current members of the room containing an interaction may use the responder there.
It applies to canonical, managed, and ad-hoc rooms.
It does not control whether an invitation is accepted.

When an agent omits its `access` block, membership in its configured managed rooms remains the default conversation grant.
A user who invites that agent into another room but lacks the configured-room grant may share a room with the agent but may not use it.
`access.users` and `access.members_of_rooms` also remain conversation grants and are not inbound-inviter allowlists.

## Valid invitation boundary

Only an authenticated Matrix self-membership event with membership `invite` and a state key equal to the bot account may start invite work.
Room metadata and membership events targeting another account are not invitations to this bot.
The authenticated event sender is the inviter checked by a list-valued `accept_invites` policy.

## Intended flow

1. Receive a valid self-invite event.
2. Persist the existing pending invite record before background work starts.
3. Acquire the existing per-room invite lock.
4. Read the latest `accept_invites` policy and evaluate the authenticated inviter.
5. Join once when the policy allows it.
6. Run the existing joined-room setup.
7. Persist the room as accepted so ordinary restart and cleanup behavior preserves it.
8. Send the existing welcome message as best effort.
9. Let all later activity use ordinary responder authorization.

## Complexity boundaries

This PR must not add a durable invite state machine, invite generations, revisions, compensating leaves, recovery workers, retry scheduling, or new cleanup ownership.
The existing pending-invite and accepted-room stores keep their current schemas and limited responsibilities.
The implementation must not use `current_room_members`, `access.users`, `access.members_of_rooms`, administrator status, or internal-agent status to decide router or agent invitation acceptance.
The implementation may reuse existing pending-work recovery, join fencing, room locking, persistence, and cleanup without strengthening their guarantees.

## Accepted failures

A process stop, cancellation, temporary Matrix failure, or revoked invitation may require another invitation.
A crash after Matrix joins but before accepted-room persistence may require cleanup or reinvitation.
Welcome delivery is best effort and receives no new retry guarantee.
Configuration changes made while a Matrix join request is already in flight may apply only to later invitations.
This PR does not guarantee exact convergence for overlapping cancellation, departure, replacement invitation, persistence failure, or restart timing.
These outcomes are non-blocking unless they bypass the configured invitation policy or post-join responder authorization.

## Required tests

1. `false` rejects invitations.
2. `true` accepts invitations without consulting responder access.
3. An exact list entry accepts its inviter.
4. A wildcard list entry accepts a matching inviter.
5. A nonmatching or empty list rejects its inviter.
6. Alias resolution occurs before list matching.
7. The router, a shared agent, and a requester-private agent use the same invitation-policy semantics.
8. A valid invitation may be accepted even when the inviter lacks the agent's configured-room conversation grant.
9. That inviter remains unable to use the agent after joining when ordinary responder authorization denies them.
10. Non-membership metadata, non-invite membership, and membership targeting another account create no invite work.
11. Successful joins retain the existing setup and accepted-room persistence behavior.
12. Existing team invitation behavior remains unchanged.

## Review contract

A review finding is blocking when it violates this contract, bypasses invitation policy, bypasses post-join responder access, breaks the normal invitation path, or adds prohibited lifecycle machinery.
A finding that only demonstrates an explicitly accepted failure is a proposal to expand scope and must not trigger implementation without a contract change.

## Definition of done

The router, shared agents, and requester-private agents can be invited according to their own `accept_invites` policy.
Post-join responder authorization remains unchanged.
The runtime implementation is materially smaller than the current router-bootstrap approach.
Focused tests, the full repository suite, and repository hooks pass.
Two fresh independent full reviews approve the same exact implementation head against this contract.
