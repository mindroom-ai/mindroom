# ISSUE-200: Streamed Interactive Replies Missing Reaction Buttons

## Fresh Live Repro Evidence

Bas requested a fresh interactive-question repro in the same thread after the original audio-modernization case. The assistant reply was parsed and transformed into numbered fallback text, but no Matrix emoji reactions/buttons appeared in Cinny.

- Room: `!xyz918dU95QMcNoeQS:mindroom.chat`
- Thread: `$WvmmRSz20ZFYVUqS77iqMXjRQ8LPED8Z6yJbMU3ju64`
- Message/root event: `$fKPJ8woWKQ8ajRvwRmIb24ILWG8jE9V1SAbx8ZdTGZY`
- Latest edit event: `$jUsH0BRJ7nkC7ZO3m8yo4iCBZ_cTouZSBuAgrsZlPY0`
- Visible body after transform: `Interactive-question repro test: do the emoji reaction options show up on this message?`
- Options: `✅`, `❌`, `🧪`

This confirms the bug reproduces on a fresh streamed assistant reply, not only on the original linked answer.

## Finding

The streaming terminal path already transformed the raw ```interactive JSON block into the visible fallback text before sending the final Matrix edit. Later, `DeliveryGateway.finalize_streamed_response()` reparsed the already-visible fallback text to decide whether post-response effects should register interactive options.

Because the fallback text no longer contains the JSON block, `option_map` and `options_list` were empty. `apply_post_response_effects()` therefore skipped `register_interactive` and never called `interactive.add_reaction_buttons()`.

## Fix

Fix commit: `2e297ea7a59d13b646723172e009f22db32ae893`

`DeliveryGateway` now derives interactive metadata from the canonical streamed final body when, and only when, formatting that canonical body exactly matches the visible streamed body. This preserves the successful streamed interactive case while still avoiding hidden canonical metadata when the terminal update failed or the visible text does not match.

The reaction registration still uses `FinalDeliveryOutcome.final_visible_event_id`, which remains the displayed/root event ID for streamed edits, not the later Matrix edit event ID.

## Validation

Focused tests run:

```text
uv run pytest -n 0 \
  tests/test_streaming_finalize.py::test_streamed_interactive_final_reply_registers_reactions_on_root_event \
  tests/test_streaming_behavior.py::TestStreamingBehavior::test_streamed_success_noop_final_transform_uses_matching_visible_interactive_metadata \
  tests/test_streaming_finalize.py::test_transport_failed_terminal_update_ignores_hidden_canonical_interactive_metadata
```

Result: `3 passed, 1 warning in 14.83s`.

Additional checks:

```text
uv run ruff check src/mindroom/delivery_gateway.py tests/test_streaming_behavior.py tests/test_streaming_finalize.py
```

Result: `All checks passed`.

## Live Retest Target

After deployment, retest with a streamed assistant reply in room `!xyz918dU95QMcNoeQS:mindroom.chat`, thread `$WvmmRSz20ZFYVUqS77iqMXjRQ8LPED8Z6yJbMU3ju64`, using the fresh repro prompt/options above. Expected result: visible fallback text plus `✅`, `❌`, and `🧪` reactions attached to the message/root event, not to the latest edit event.

## Regression origin

Regression was introduced by PR #687's final squash on `origin/main`:

- Commit: `5cda1ce1837dc4ed710ef549aad001a08bc116ea`
- Tag: `v2026.4.232`
- Commit time: 2026-04-24 00:32:13 -0700
- Subject: `fix: finalize delivery contract, harden streamed terminal status (ISSUE-178  + ISSUE-181) (#687)`

Narrower internal PR-history source: R5 commit `853dd8990` (`fix(pr687-r5): derive interactive metadata from visible text`, 2026-04-23 06:58 PDT) changed streamed finalization to parse the already-visible `streamed_text` instead of the canonical/raw final body candidate. That made sense as a hidden-metadata safety move, but it also dropped the raw JSON `interactive` block after streaming had already converted it to fallback text.

A later PR-branch commit `5adac24ee` (`Preserve streamed interactive metadata`, 2026-04-23 11:45 PDT) had the same helper shape as the ISSUE-200 fix, but the final PR #687 squash still landed with the bad `parse_and_format_interactive(streamed_text, ...)` behavior in `DeliveryGateway.finalize_streamed_response()`. So for production/root-cause purposes the regression entered `origin/main` via `5cda1ce18`.

## PR #687 lost-work audit

After noticing the ISSUE-200 fix shape existed in PR-687 history but not in the final squash, Bas asked whether anything else was accidentally lost.

Method:

- Compared `5cda1ce18` (final PR #687 squash on `origin/main`) with local preserved branch `pr-687`.
- Checked production-code delta with `git diff --name-only 5cda1ce18..pr-687 -- ':!tests/**' ':!docs/**' ':!skills/**'`.
- Traced the `_interactive_response_for_visible_body` helper with `git log -S`.

Findings:

1. Production-code delta between final squash and preserved `pr-687` tip is empty. `git diff --name-only ... -- ':!tests/**' ':!docs/**' ':!skills/**'` returned no files. So there is no broad production-code loss between final squash and the preserved PR branch tip.
2. Remaining `5cda1ce18..pr-687` diff is only:
   - `docs/superpowers/plans/2026-04-22-streaming-terminal-simplification.md` (+482, design/working plan)
   - `tests/test_streaming_behavior.py` (+20/-6, test-fixture modernization)
   This looks non-critical and not a runtime regression source.
3. The interactive metadata helper was introduced in PR branch commit `5adac24ee` (2026-04-23 11:45 PDT) and removed later by `cf6420c25` (2026-04-23 16:50 PDT, `refactor(pr687-r8): make finalize_streamed_response total, delete recovery wrapper`). That refactor rewrote `finalize_streamed_response()` around a total try/except shape and dropped the helper. This means the ISSUE-200 regression was not lost only at squash time; it was lost during the PR-branch R8 refactor, then the final squash faithfully preserved the post-R8 bad behavior.
4. Earlier `git cherry` shows many PR-branch commits as unmatched because PR #687 was squash-merged, but the file-level diff against preserved `pr-687` is the meaningful check. Aside from the docs/test-only residual diff above, the final squash matches the preserved branch tip in runtime code.

Conclusion: No evidence of additional runtime fixes lost from PR #687's final squash beyond the streamed interactive metadata guard already fixed by ISSUE-200. The exact loss point was PR-687 R8 commit `cf6420c25`, not the GitHub squash itself.
