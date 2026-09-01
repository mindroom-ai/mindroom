# Clear Authority Boundaries Plan

## Status

This is the living contract and implementation plan for the authority-boundary PR.
The implementation, tests, documentation, and review findings must be evaluated against this document.
This document must be deleted in the final commit before merge.

## Goal

Make MindRoom authority easy to understand by giving each configuration option one responsibility.
Fix the small places where current behavior violates that model without introducing a general roles system or a new lifecycle mechanism.

## Product model

MindRoom has five independent authority questions.

| Question | Configuration or runtime owner |
| --- | --- |
| Who may make a bot join a room? | `<entity>.accept_invites` |
| Who may interact with a responder? | `<entity>.access` |
| Whose state and credentials does an interaction use? | The canonical `requester_id` plus the agent's `private` and worker scope |
| Who may administer platform or credential configuration? | `administrators` and `agents.<name>.credential_managers` |
| Who may execute one sensitive tool action? | Tool availability and tool approval policy |

No row grants another row.
Joining a room does not grant conversation access.
Conversation access does not grant credential management.
Credential management does not grant conversation access.
Requester-private state does not grant access to the agent.
Tool approval is an additional action gate and does not replace conversation access.

## Identity boundary

Every inbound Matrix turn has two identities.

- `transport_sender_id` is the exact authenticated Matrix account that sent the event.
- `requester_id` is the canonical principal that owns the resulting conversation, state, requester-scoped credentials, approvals, triggers, scripts, attachments, and usage.

The ingress boundary must select the trusted requester first and then use the shared human-requester resolver.
Managed responders, configured bot accounts, and MindRoom's internal account must retain their transport identity even if a malformed runtime configuration lists them as aliases.
Alias configuration must reject chains, cycles, and self-aliases so canonicalization is idempotent at downstream policy boundaries.
The exact transport sender remains available through `TurnOrigin` for Matrix provenance, membership, and transport-specific behavior.
Downstream ownership code must receive the canonical requester.
Every independent event adapter, including messages, reactions, approval actions, invitations, and dashboard requests, must resolve the canonical requester before creating ownership or execution records.
Raw room-membership rosters must use the same resolver when matching configured aliases.

This change intentionally makes one human use one requester-owned scope when they enter through a canonical Matrix account or a configured bridge alias.
It must not trust unverified original-sender metadata or turn a managed bot account into a human requester.

## Invitation authority

The router, agents, and teams use the same `accept_invites` policy.

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

A list accepts inviters whose canonical Matrix user ID matches at least one case-sensitive pattern.
An empty list rejects every invitation.
The existing invite lifecycle and its deliberately limited recovery guarantees remain unchanged.
This PR only removes the legacy team exception that used conversation access as invitation authority.

## Conversation authority

Responder `access` has three independent allow clauses.

- `users` allows exact canonical Matrix user IDs or case-sensitive patterns.
- `current_room_members: true` allows authoritative joined members of the room containing the interaction.
- `members_of_rooms` allows authoritative joined members of the named managed grant rooms.

The clauses use OR semantics.
Internal MindRoom identities and platform administrators remain separate bypasses.
Invited users do not count as joined members.
Missing, stale, or unavailable authoritative membership fails closed.

When an agent or team omits `access.members_of_rooms`, its managed `rooms` remain the default grant rooms.
When the router omits `access.current_room_members`, the default remains `true`.
An explicit empty `members_of_rooms` list disables inferred grant rooms.
The generated starter configuration will author the effective agent access explicitly so the common case does not depend on remembering these defaults.

The documentation will call `members_of_rooms` entries managed grant rooms instead of using the informal term canonical room.

## Administration and credentials

Top-level `administrators` are platform administrators.
They bypass responder access and may administer platform configuration and shared credentials, but they do not receive Matrix room power or invitations automatically.

`agents.<name>.credential_managers` may manage the named shared agent's credentials and OAuth connections.
This does not allow them to converse with the agent or use its credentials.
Conversation-authorized users may ask an enabled tool to use credentials available to the selected execution scope, subject to tool approval.
Authenticated requesters may manage their own requester-private OAuth connection without being static credential managers.

Platform administrators must automatically count as external-trigger administrators.
`external_trigger_policy.admin_users` remains an additive list for people who should administer triggers without receiving wider platform authority.
Both trigger management entry points must use one shared predicate with alias resolution.

## Requester-private state

The `private` agent field means requester-private state placement, not permission to use the agent.
Responder `access` is checked before a requester-private instance is selected.
The existing YAML field remains unchanged to avoid a cosmetic rename with broad churn.
User-facing documentation and labels should consistently say requester-private state.

## Tool approval and resource ownership

Tool approval remains bound to the exact canonical requester who initiated the action.
Approval rechecks current responder access and cannot be supplied by an unrelated administrator.

Schedules remain room-managed resources for simplicity.
External triggers, background scripts, attachments, requester-private workers, and requester-scoped credentials remain requester-owned.
Canonical requester identity makes that ownership consistent across configured bridge aliases.
This PR does not add creator-only schedule permissions or new resource ACLs.

## Selected implementation

The implementation has four production changes.

1. Put human requester classification and alias resolution in one leaf policy module, use it at every Matrix event adapter, and retain the raw transport sender in `TurnOrigin`.
2. Add `accept_invites` to `TeamConfig` and remove the room-lifecycle branch that authorizes legacy team invitations through responder access.
3. Make platform administrators and additive trigger administrators share one alias-aware external-trigger administrator predicate.
4. Route raw-ID policy plus requester-owned OAuth and dashboard boundaries through the same human-requester resolver without changing credential storage or lifecycle semantics.

The remaining work is documentation, generated starter clarity, and focused tests.

## Non-goals

- Do not build general RBAC, roles, deny rules, nested Boolean policy expressions, or per-user tool allowlists.
- Do not couple invitation policy back to responder access.
- Do not add invitation states, generations, recovery workers, rollback logic, or stronger crash guarantees.
- Do not rename existing YAML fields solely for aesthetics.
- Do not redesign Matrix power levels, room invitations, OAuth storage, schedules, approvals, or resource stores.
- Do not add dashboard editors for authority fields in this PR.
- Do not migrate old requester-owned state between alias-derived and canonical paths.

## Required tests

1. A direct event from a configured bridge alias returns the canonical requester while preserving the alias as the transport sender.
2. A trusted internal relay with an aliased original human returns the canonical requester while preserving the managed relay account as the transport sender.
3. Untrusted original-sender metadata remains ignored.
4. Canonical requester identity reaches a requester-owned runtime scope without another alias decision.
5. Approval reactions and denial replies preserve the transport sender but compare ownership as the canonical human requester.
6. Interactive reactions record and execute as the canonical requester without losing Matrix transport provenance.
7. Dashboard credential targets use the same canonical requester as Matrix and OAuth runtime paths.
8. Teams parse `true`, `false`, exact-pattern lists, wildcard lists, and empty invitation lists.
6. A team accepts and rejects inviters through `accept_invites` independently from responder `access`.
7. Router, agent, and team invitation policies use the same pure evaluator.
8. A platform administrator can manage external triggers without appearing in `external_trigger_policy.admin_users`.
9. An additive trigger administrator can manage triggers without platform-wide authority.
10. A configured alias receives the same trigger-administrator result as its canonical principal.
11. Revoking both forms of trigger administration takes effect through the current config provider.
12. The generated starter configuration explicitly authors its effective conversation access.
13. Managed responders, configured bot accounts, and MindRoom's internal account cannot be remapped into human requesters.
14. Alias chains, cycles, and self-aliases are rejected so repeated policy resolution remains idempotent.
15. A configured bot alias cannot inherit responder, administrator, invitation, trigger, or OAuth ownership authority from a human.

## Implementation plan

### Task 1: Canonical requester identity at ingress

**Files:**

- Modify `tests/test_ingress_validation.py`.
- Modify `tests/test_turn_controller_focused.py` to prove the canonical requester reaches the response envelope used for requester-owned scope selection.
- Modify `src/mindroom/ingress_validation.py`.
- Create `src/mindroom/requester_identity.py` as the shared leaf policy owner.

- [x] Write failing tests for direct aliases, trusted relayed aliases, preserved transport identity, and requester-owned scope reuse.
- [x] Run the focused tests with `uv run pytest -n auto` and confirm the new expectations fail.
- [x] Resolve aliases only after the trusted requester has been selected.
- [x] Route raw-ID policy and requester-owned storage boundaries through the shared human-only resolver.
- [x] Keep `event.sender` as `TurnOrigin.transport_sender_id`.
- [x] Run the focused ingress, turn-origin, runtime-resolution, and private-identity tests.
- [x] Commit the independently testable identity boundary change.

### Task 2: Give teams the same invitation policy

**Files:**

- Modify `src/mindroom/config/agent.py`.
- Modify `src/mindroom/matrix/invited_rooms_store.py`.
- Modify `src/mindroom/bot_room_lifecycle.py`.
- Modify focused access and invite tests.

- [x] Write failing configuration and lifecycle tests for team boolean and pattern-list policies.
- [x] Run those tests with `uv run pytest -n auto` and confirm the missing policy fails.
- [x] Add `TeamConfig.accept_invites` with the shared policy type and `true` default.
- [x] Return the team policy from the shared invitation-policy resolver.
- [x] Make the lifecycle use the dedicated invitation policy for every entity.
- [x] Delete the legacy team responder-access branch and its imports.
- [x] Run the focused invite, authorization, access-schema, and orchestrator tests.
- [x] Commit the independently testable invitation symmetry change.

### Task 3: Unify external-trigger administrator authority

**Files:**

- Create `src/mindroom/external_triggers/policy.py` for the shared administrator predicate.
- Modify `src/mindroom/custom_tools/external_trigger_manager.py`.
- Modify `src/mindroom/external_triggers/store.py`.
- Modify `tests/test_external_trigger_manager_tool.py`.
- Modify `tests/test_external_trigger_store.py`.

- [x] Write failing tests for platform administrators, additive trigger administrators, aliases, and live revocation.
- [x] Run those tests with `uv run pytest -n auto` and confirm platform-administrator and alias cases fail.
- [x] Implement one pure alias-aware trigger-administrator predicate.
- [x] Use it for cross-target creation, listing, mutation, key rotation, and deletion.
- [x] Run the focused external-trigger policy, manager, store, and API tests.
- [x] Commit the independently testable trigger authority change.

### Task 4: Make the authored model explicit

**Files:**

- Modify `src/mindroom/cli/config.py`.
- Modify `tests/test_cli_config.py`.
- Modify `docs/authorization.md`.
- Modify `docs/configuration/agents.md`.
- Modify `docs/configuration/teams.md`.
- Modify `docs/configuration/index.md` and `docs/dev/agent_configuration.md` where they summarize team invitation configuration.
- Modify `docs/configuration/router.md` only if its shared invitation wording becomes inaccurate.
- Modify `docs/oauth-framework.md`.
- Modify `docs/external-triggers.md`.
- Modify `docs/scheduling.md` only to clarify existing room ownership where its current wording is ambiguous.

- [x] Add a failing generated-config assertion for explicit agent access.
- [x] Author the starter agent's effective `current_room_members`, `members_of_rooms`, and `users` values.
- [x] Document the five authority questions and their independence.
- [x] Document OR semantics, hidden defaults, managed grant rooms, and requester-private state.
- [x] Correct OAuth documentation so requester-private self-management matches runtime behavior.
- [x] Document platform trigger administrators and additive `admin_users`.
- [x] Add `accept_invites` to team configuration documentation.
- [x] Keep one sentence per Markdown source line.
- [x] Run generated documentation checks and focused config tests.
- [x] Commit the documentation and starter-configuration clarification.

### Task 5: Verify the exact PR head

- [x] Run every affected Python test file with `uv run pytest -n auto`.
- [x] Run `uv run pre-commit run --all-files` after `uv sync --all-extras` in this worktree.
- [x] Run the full suite with `uv run pytest -n auto`.
- [x] Inspect the production diff and remove any abstraction or branch not required by this contract.
- [x] Verify invitation lifecycle code did not grow beyond the team-policy simplification.
- [ ] Run `git status --short` before staging.
- [ ] Scan all proposed public content and likely generated references for prohibited private identifiers.
- [ ] Stage only explicit intended paths and inspect the staged diff.
- [ ] Commit without amending and push the exact verified head.
- [ ] Run a fresh full PR review against this contract and validate every finding before changing code.

### Task 6: Remove this temporary document

- [ ] Confirm implementation, tests, user documentation, and PR description fully preserve the contract without relying on this file.
- [ ] Delete `docs/dev/authority-boundaries-plan.md` in its own final pre-merge commit.
- [ ] Push the deletion commit and verify the PR diff contains no temporary planning artifact.

## Progress

- [x] Reconstructed the current authority model from code, tests, documentation, and merged invitation policy.
- [x] Selected the minimal boundary correction and rejected a general policy framework.
- [x] Defined explicit production changes, accepted semantics, non-goals, and required tests.
- [x] Task 1 canonicalizes trusted requesters once at Matrix ingress while preserving transport identity.
- [x] Task 2 gives teams the same dedicated invitation policy and removes their lifecycle exception.
- [x] Task 3 makes platform and additive external-trigger administrators use one alias-aware predicate.
- [x] The scoped implementation is complete.
- [x] Third-round review aligned grant-room rosters, approval actions, interactive reactions, and dashboard credentials with the shared human-requester boundary.
- [ ] Whole-PR verification and review remain.
