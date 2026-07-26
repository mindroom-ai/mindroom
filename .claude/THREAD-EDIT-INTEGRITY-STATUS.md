# PR #1641 thread-edit integrity status

## Active exact-head full-suite correction

- PR: `https://github.com/mindroom-ai/mindroom/pull/1641`
- Branch: `fix/thread-edit-integrity`
- Rejected local, remote, and GitHub head: `2cbb411b514c279e28c625761eca81db6c10453e`
- Current base and merge base: `858282afc77adb480fa06cd9e4057d511ff861d5`
- Never amend, force-push, or merge.
- Verify `Bas Nijholt <bas@nijho.lt>` before every commit.

Exact `2cbb411b5` full pytest completed with `12264` tests, `54` skipped, `12` failures, and no errors.
Nine failures were deterministic thread-preview regressions.
Three unrelated CLI assertions passed serially with a wider terminal and were Rich output wrapping only.

Root cause:

- Production bundled preview selection now correctly consumes `ConversationEventCache.get_latest_edit()`.
- The shared `make_event_cache_mock()` still hard-coded `get_latest_edit()` to return `None`.
- Existing preview regressions therefore stopped exercising the production cache contract and returned original bodies.

Strict TDD evidence:

- Existing tests failed `9/9` both under xdist and serial execution.
- The correction is test-only: make the shared cache mock select bundled candidates using the canonical `ordered_replacements()` contract.
- Do not add a production fallback around the cache API.

Next:

1. Prove the exact nine failures green.
2. Run affected owning files and the full suite.
3. Commit and push the test-only correction.
4. Remove this handoff and freeze a new exact head.
5. Restart exact-head Codex, CI, full/Tach/all-file, and real-Tuwunel gates.

Any commit invalidates all earlier review, CI, full-suite, PostgreSQL, and live evidence.
Preserve every failure artifact.
Remove this handoff only before the next final exact-head freeze.
Never merge.
