# Independent Invite Policy Implementation Plan

> Execute this plan test-first and keep each change minimal.
> This plan and `invite-acceptance-contract.md` are temporary development artifacts and must be deleted in the final commit before merge.

**Goal:** Make `accept_invites` the sole inbound invitation policy for the router and agents while leaving responder conversation access unchanged.

**Architecture:** Extend `accept_invites` from a boolean to a boolean-or-pattern-list policy.
Evaluate that policy at the existing room-lifecycle join boundary.
Remove invite-time responder authorization and the process-local bootstrap machinery from the current PR.
Keep existing pending work, room locking, join fencing, accepted-room persistence, and team behavior unchanged.

**Authoritative contract:** `docs/dev/invite-acceptance-contract.md`

## Global constraints

- Run every pytest command with `-n auto`.
- Use the same alias resolution and case-sensitive wildcard semantics as responder `access.users`.
- Do not change responder authorization behavior.
- Do not add persistence fields, invite tokens, lifecycle states, retry workers, compensating leaves, or cleanup branches.
- Preserve unrelated work and stage only explicit paths.
- Update the contract before changing a guarantee or accepted failure.

## Task 1: Define configuration semantics

**Files:**
- Modify `src/mindroom/config/access.py` or another existing leaf config module for the shared policy type and validation helper.
- Modify `src/mindroom/config/agent.py` for agent configuration.
- Modify `src/mindroom/config/models.py` for router configuration.
- Modify focused configuration tests.

1. Add tests that parse `true`, `false`, exact-ID lists, wildcard lists, and empty lists for both router and agent configuration.
2. Run those tests and confirm the list forms fail before production changes.
3. Add a shared `bool | list[str]` invitation-policy type with unique pattern validation.
4. Preserve `true` as the default.
5. Run the focused tests and confirm all forms pass.

## Task 2: Define invitation-policy evaluation

**Files:**
- Modify `src/mindroom/matrix/invited_rooms_store.py` or a more focused existing invite-policy leaf.
- Modify focused invite-policy tests.

1. Add tests for boolean policy, exact matching, wildcard matching, alias resolution, empty lists, and nonmatching lists.
2. Run the tests and confirm they fail for the missing evaluator.
3. Implement one pure evaluator for router and agent inviters.
4. Keep the existing broad `should_accept_invites` helper for non-sender-specific callers by treating a nonempty list as enabled.
5. Run the focused evaluator tests.

## Task 3: Simplify the join boundary

**Files:**
- Modify `tests/test_room_invites.py`.
- Modify `src/mindroom/bot.py`.
- Modify `src/mindroom/bot_room_lifecycle.py`.
- Restore `src/mindroom/authorization.py` to its baseline responsibilities.

1. Add or change tests proving the router, a shared agent, and a requester-private agent accept valid invitations according to `accept_invites` without consulting responder access.
2. Add a test proving an accepted inviter can still be denied by ordinary post-join responder authorization.
3. Add tests proving exact and wildcard invitation lists allow matching senders and reject other senders.
4. Add callback-boundary tests for valid self-membership invitations and irrelevant Matrix state events.
5. Run the tests and confirm the corrected behavior fails on the current router-bootstrap implementation.
6. Remove the live invite token, bootstrap helper, post-join membership query, and invite-time responder authorization for router and agents.
7. Evaluate the latest invitation policy under the existing room lock immediately before the existing fenced join.
8. Retain existing setup, persistence, welcome, pending-work, and team behavior.
9. Run the focused invitation and authorization suites.

## Task 4: Align documentation and generated references

**Files:**
- Modify invite-related user documentation.
- Regenerate checked-in MindRoom documentation references using the repository workflow.

1. Document all three `accept_invites` forms.
2. State explicitly that invitation acceptance and conversation authorization are independent.
3. Remove router-bootstrap and invite-time `current_room_members` language.
4. Confirm one sentence per Markdown source line.

## Task 5: Verify and publish the exact review head

1. Run the complete affected test surface with `-n auto`.
2. Run `uv run pre-commit run --all-files`.
3. Run `uv run pytest -n auto`.
4. Inspect source diff size and remove leftover bootstrap machinery or tests.
5. Run `git status --short` before staging.
6. Scan proposed public content and generated references for prohibited private identifiers.
7. Stage only intended paths and inspect the staged diff.
8. Commit without amending and push the exact verified head to PR 1931.
9. Update the PR title and body to the independent invite-policy contract.
10. Request two fresh independent full reviews of the same exact implementation head.
11. Delete both temporary development documents in the final commit before merge.

## Progress

- [x] The original product regression and configuration ownership were reconstructed from the first commit of PR 1925.
- [x] The contract now separates inbound invitation policy from post-join responder access.
- [x] Task 1 added and validated boolean-or-pattern-list configuration semantics.
- [x] Task 2 added and validated exact, wildcard, empty-list, boolean, and alias-aware inviter evaluation.
- [x] Task 3 removed invite-time responder authorization and process-local bootstrap machinery while preserving the existing join flow.
- [x] Task 4 aligned user documentation and regenerated the checked-in documentation references.
- [x] Task 5 passed the affected test surface, the full repository suite, and all repository hooks.
- [ ] Two fresh independent full reviews remain before the final deletion-only commit.
