# PR #1641 thread-edit integrity handoff

## Current exact state

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Published candidate before this review round: `4e925fbae10de9f795bfd25ce2a3b44c25cf8454`
- Base and merge-base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Git author required before every commit: `Bas Nijholt <bas@nijho.lt>`
- Never amend, force-push, merge, or use temporary directories for durable work.

## Exact-head evidence now invalidated

Exact `4e925fbae` passed full pytest (`12304`, 54 skipped), the seven PostgreSQL stress tests, all-file pre-commit, Tach, and `git diff --check`.
Those results remain useful diagnostic evidence but cannot certify a later head.
Real Tuwunel has not run on `4e925fbae`.

## Active review round

Fresh exact-head review reported four candidate blockers that must be independently reproduced before production edits:

1. Full-history ordering retains stale opaque or provisional representations after a canonical same-ID upgrade.
2. Generic visible resolution observes top-level identities before bundled identities, allowing one edit ID to target two originals.
3. Room-history scan has the same bundled-identity gap before per-thread grouping.
4. Cache batch admission derives indexes and sidecar ownership from intermediate accepted representations instead of one final canonical representation.

Use strict RED-GREEN TDD for every confirmed blocker.
Keep one shared identity-canonicalization source of truth and avoid backend-specific workarounds.

## Final gates

After the last code commit:

- Remove this file in a dedicated final commit.
- Run fresh independent exact-head Codex reviews.
- Require terminal green GitHub CI and zero unresolved current review threads.
- Run full pytest, relevant SQLite/PostgreSQL backend tests, all-file pre-commit, Tach, and `git diff --check`.
- Read and run the preserved real-Tuwunel procedure on the same exact head, retaining artifacts.
- Any code change invalidates all reviews and gates.
