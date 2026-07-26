# PR #1641 thread-edit integrity status

## Active exact-head rejection

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Rejected head: `b2fd83c97de10415e0fa5742eda2dfe7ae9f5f75`
- Base and merge base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Branch: `fix/thread-edit-integrity`
- Never merge from this task.

Two fresh independent exact-head native Codex reviews returned `CHANGES REQUIRED`.

Verified review claims awaiting RED coverage:

1. A bundled replacement whose event ID equals its container can conflict with and quarantine the original cache row, while full history rejects the self-edit.
2. Invalid wrong-room or state bundled representations enter identity-conflict observation before scope validation and can quarantine or hide a valid same-ID explicit edit.
3. Room-scan and generic visible-message collection can reconcile duplicate identities too late, allowing last-wins source loss or one event ID to remain both a visible message and a replacement.

## Evidence before rejection

- Exact-head full pytest passed `12278` tests with `54` skipped and zero failures or errors.
- Exact-head all-file pre-commit, Tach dependencies/interfaces, and `git diff --check` passed.
- The explicit seven-test PostgreSQL stress selection passed.
- GitHub had zero unresolved review threads and no failed checks; CI was still running.
- No real-Tuwunel run was started.

All evidence above is stale for the next code or test head.

## Required TDD sequence

1. Independently verify each review claim against exact source.
2. Add deterministic RED regressions at full-resolution/room-scan, stale-cleanup, SQLite, and PostgreSQL owning seams.
3. Implement one room-scoped canonical identity observation contract at the owning seam.
4. Run focused RED-to-GREEN tests, owning backend suites, broad consumers, full pytest, Tach, and all-file pre-commit.
5. Verify Bas Nijholt `<bas@nijho.lt>` immediately before every commit.
6. Push only small normal commits; never amend or force-push.
7. Remove this file only after the final handoff-free exact candidate is frozen.
8. Restart fresh exact-head Codex, CI, and isolated real-Tuwunel gates after every code or test commit.

## Preserved inputs

- Harness SHA-256: `c91168f32354ebd142120158d60761ff6929927d02d4fcf6f131873d79ac755b`
- Scenario SHA-256: `6cb616b3c367f6b523f1e78b10196db3851baec640e9057d14533025eab8bfb4`
- nio head: `e15f9e19ecbb8645564373d6c0fe7f7ffe06076f`
- Preserve all logs, databases, receipts, and failure artifacts under persistent storage.
