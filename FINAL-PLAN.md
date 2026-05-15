# ISSUE-216 — FINAL PLAN: Kill the redundant 32K compaction cap

**Synthesized from:** Codex `PLAN.md` (commit `13872caf5`) + Claude `PLAN-B.md` (commit `cb7519770`) + cross-critiques (`4a6715aff` Codex-on-Claude, `5414775ec` Claude-on-Codex).

**Verdict:** Both critiques `APPROVE_WITH_NITS` and converge on the same synthesis below.

## TL;DR

Delete `_effective_summary_input_budget_tokens()` and the now-vestigial `compaction_context_window` parameter from the entire compaction rewrite call chain. Add ONE focused regression test that asserts the property "rewrite passes the full `summary_input_budget` into chunk construction." Defer low-water-mark to ISSUE-217 (to be filed). Total: ~−25 / +27 LOC across 3 files.

## 1. Source changes

### 1.1 `src/mindroom/history/compaction.py`

**Delete entirely (lines 965-970):**
```python
def _effective_summary_input_budget_tokens(summary_input_budget: int, compaction_context_window: int | None) -> int:
    """Return the conservative per-call summary input budget."""
    if compaction_context_window is None or compaction_context_window <= 0:
        return summary_input_budget
    per_call_cap = max(2_000, min(compaction_context_window // 4, 32_000))
    return min(summary_input_budget, per_call_cap)
```

**Replace call site (compaction.py:433-436)** in `_rewrite_working_session_for_compaction()`:
```python
# before
per_call_summary_input_budget = _effective_summary_input_budget_tokens(
    summary_input_budget,
    compaction_context_window,
)
# after
per_call_summary_input_budget = summary_input_budget
```

Keep the local `per_call_summary_input_budget` name unchanged — it's used at lines 483, 491, 503 and renaming would add diff churn for no behavioral benefit.

**Drop `compaction_context_window` parameter from `_rewrite_working_session_for_compaction()` signature (line 417)** — it has zero remaining readers inside the function after the helper is deleted.

**Drop `compaction_context_window` parameter from `compact_scope_history()` signature (line 260) and pass-through (line 323)** — it only forwarded to `_rewrite_working_session_for_compaction`.

`ResolvedHistoryExecutionPlan.compaction_context_window` (`types.py:68`) and its consumers in `policy.py` STAY — the field is still legitimately used upstream by `_resolve_summary_input_budget()` to compute `summary_input_budget_tokens`. Only the per-call cap consumer is gone.

### 1.2 `src/mindroom/history/runtime.py`

**Drop `compaction_context_window=execution_plan.compaction_context_window,` kwarg from `compact_scope_history()` call at line 647.**

### 1.3 Floor (`max(2_000, ...)`)

**Drop with the rest of the helper.** Already-redundant safety net: `_resolve_summary_input_budget` in `policy.py:189-207` rejects non-positive budgets upstream and surfaces `non_positive_summary_input_budget` to the caller before compaction is even attempted. No live model in `~/.mindroom-chat/config.yaml` has a window small enough for the floor to engage.

## 2. Test changes — `tests/test_agno_history.py`

### 2.1 Deletions

- **Delete `test_effective_summary_input_budget_caps_per_chunk` (lines 1101-1106).** Sole purpose was asserting the cap we are removing.
- **Delete `_effective_summary_input_budget_tokens` import (line 60).**
- **Delete the `compaction_context_window=` kwarg from 6 call sites** (mechanical, no logic change):
  - 4 sites passing into `_rewrite_working_session_for_compaction(...)`: lines `1154`, `3577`, `3635`, `3826`
  - 2 sites passing into `compact_scope_history(...)`: lines `1612`, `3727`

### 2.2 New regression test (Codex's, refined)

Add an integration-level test on `_rewrite_working_session_for_compaction` that locks in the property "a healthy single call uses the full `summary_input_budget`, NOT a hidden cap." This is the property PR #856 silently broke and that none of the existing tests would catch if anyone re-introduces a per-call cap by any name.

Sketch (≈25 LOC):

```python
@pytest.mark.asyncio
async def test_rewrite_uses_full_summary_input_budget_without_per_call_cap(tmp_path: Path) -> None:
    """Regression for ISSUE-216: a healthy compaction folds many runs in ONE
    summary call when the summary_input_budget is large enough to fit them.
    PR #856 added a hidden 32K cap that broke this; this test prevents
    re-introducing any equivalent cap by name, env var, or 'safety' min().
    """
    # Build 5 completed runs whose serialized form is each ~10K tokens.
    # Use forced compaction or available_history_budget=None so selection picks all of them.
    # Patch _generate_compaction_summary to record the input it received.
    # Call _rewrite_working_session_for_compaction(..., summary_input_budget=70_000).
    # Assert: exactly 1 call to the patched generator, AND
    #         len(captured_included_runs[0]) >= 5,    OR equivalently
    #         rewrite_result.compacted_run_count >= 5.
```

Implementation notes for the implementer:
- Place adjacent to where the deleted `test_effective_summary_input_budget_caps_per_chunk` lived.
- Mirror the construction style of nearby `test_rewrite_*` tests (existing fixtures for AgentSession + RunOutput).
- Keep run sizes moderate (a few thousand serialized chars each) so the test stays fast.
- Mock `_generate_compaction_summary` (or `_generate_compaction_summary_with_retry`) — DO NOT call a real model.
- Assert on `compacted_run_count` if simpler than capturing summary inputs; both shapes prove the property.

### 2.3 NOT touching
- `tests/test_compact_context.py` — no references.
- `tests/test_compaction.py` — only references `compaction_context_window` on `ResolvedHistoryExecutionPlan` (still valid).
- `tests/test_extra_kwargs.py` — no actual references; verify it still passes but expect no failures.

## 3. Chunk-retry preservation (no code change required)

`_generate_compaction_summary_with_retry` at `compaction.py:994-1080` is untouched. After the fix:
- **Healthy first call:** budget ~170K (Opus), all visible runs fit, single round-trip succeeds. New common case.
- **Timeout:** `_should_retry_smaller_summary_chunk` matches `"timed out"`; retry runs with `max(1_000, 170_000 // 2) = 85_000`. Existing test `test_rewrite_retries_summary_with_smaller_chunk_after_timeout` continues to pass (uses `summary_input_budget=8_000`, blind to the deleted cap).
- **Provider context-length error:** same retry path (`"context length"` and friends in `retry_fragments`).
- **Both attempts fail:** original exception re-raised. No regression.

If post-merge live evidence shows the larger first attempt times out frequently, the operational lever is `MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS` (env var, currently 180s, no code change). **Do NOT pre-emptively raise it.**

## 4. Low-water-mark — explicit deferral

Out of scope. File **ISSUE-217** ("compaction stops at the trigger threshold; lower the post-compaction target to delay re-trigger") and link it from the PR description.

PR description must include the **quantitative re-evaluation criterion**:
> Re-prioritize ISSUE-217 if, after 7 days of `mindroom-lab` usage, any single thread shows ≥3 distinct compactions in any 24h window.

Rationale for deferring: with the cap removed, a single Opus pass already drops ~168K → ~80K (~50% reduction), which naturally overshoots a hypothetical 60% water mark. The visible "compaction every 30 min" pain likely disappears for typical sessions even without explicit low-water-mark targeting.

## 5. PR description requirements

The squash-merge commit body MUST include (in addition to standard problem/approach/why/how paragraphs per `MEMORY.md` commit-message format):

1. **One-paragraph cost note:**
   > Per-call cost is roughly 5× higher (32K → ~170K input tokens for Opus 4.7), but per-session compaction call count drops by roughly the same factor (one call folding 20 runs replaces ~10 calls each folding 2 runs). Net per-session cost is approximately flat to slightly cheaper. Worst-case spike: a timed-out larger first attempt + halved retry pays for ~7.5× one compaction's normal cost; rare and bounded.

2. **Link to ISSUE-217 follow-up** with the quantitative re-eval criterion above.

3. **Note that the chunk-retry path remains intentionally untouched** — it was correct in PR #856; only the redundant cap was wrong.

## 6. Live test plan (Phase 4 hard gate)

Deploy implementation branch to `mindroom-lab.service`, drive a long-running thread until compaction triggers naturally OR force compaction via the existing `compact_context` path. Pass criteria — ALL must hold:

1. **Per-chunk request log:** `grep "Compaction summary chunk request" mindroom_data/logs/*.log` → assert `included_runs >= 6` for the dev-agent session (target: full visible set, e.g. ~20).
2. **Compaction notice / outcome:** lifecycle progress event reports `compacted_run_count >= 6` (ideally equal to `runs_before − runs_after`).
3. **Token reduction:** `(after_tokens / before_tokens) <= 0.5` for the dev-agent session.
4. **Single pass:** exactly 1 `Compaction summary chunk request` per compaction outcome (no chunked retry, no second pass) — UNLESS the retry path engaged for a real provider error, in which case verify it engaged correctly with halved budget.
5. **Coherence (eyeball, Bas):** open the resulting `<summary_of_previous_interactions>` block in the next agent run's prompt. Verify it preserves: project context, recent file edits, exact paths/IDs/commits, open questions, decisions. Bas to validate.
6. **No re-trigger within next ~10 turns** in the same thread (loose check; ISSUE-217 will tighten this).

If (1)-(4) fail → revert. If (5) fails → revert and consider tuning `COMPACTION_SUMMARY_PROMPT` (separate ticket). If only (6) fails → ship anyway and prioritize ISSUE-217.

Capture all evidence to `/tmp/ISSUE-216-evidence/`:
- raw logs from before + after compaction (the chunk request + chunk completed + lifecycle progress events)
- screenshot or text dump of the next-turn agent prompt showing `<summary_of_previous_interactions>`
- a short diff of `runs_before / runs_after / before_tokens / after_tokens` from the lifecycle event

## 7. Diff size estimate

| File | LOC delta | Notes |
|---|---|---|
| `src/mindroom/history/compaction.py` | ≈ −10 / +1 | Delete helper (6) + docstring + blank lines; replace 4-line call site with 1; drop param from 2 function signatures |
| `src/mindroom/history/runtime.py` | ≈ −1 / 0 | Drop `compaction_context_window=` kwarg from `compact_scope_history` call |
| `tests/test_agno_history.py` | ≈ −13 / +25 | Delete cap test (6) + import (1) + 6 mechanical kwarg removals; add new regression test (~25 LOC) |
| **Total** | **≈ −24 / +26** | **3 files: 2 source + 1 test** |

## 8. Hard rules for the implementer

- **Branch:** `issue-216` off `origin/main` (already created with this FINAL-PLAN as first commit).
- **Push only to `gitea` remote.** Never push to `origin`.
- **Run focused tests via nix-shell:**
  ```
  nix-shell --run 'uv run pytest tests/test_agno_history.py -x -n 0 --no-cov -v'
  nix-shell --run 'uv run pytest tests/test_compaction.py tests/test_compact_context.py tests/test_extra_kwargs.py -x -n 0 --no-cov -v'
  ```
- **Pre-commit:** `pre-commit run --all-files` must pass.
- **Commit format:** subject ≤72 chars; body has ≥3 paragraphs (problem / approach / why-or-how); one sentence per line; no test counts, no reviewer names, no file enumerations. The cost note + ISSUE-217 reference belong in the eventual PR description, not the commit body.
- **Out of scope (do NOT touch):** the chunk-retry logic, `COMPACTION_SUMMARY_PROMPT`, the low-water-mark exit condition, prompt caching, anything outside `src/mindroom/history/`.
- **Stop conditions:** if any of (a) the focused tests don't pass after a reasonable fix attempt, (b) the diff scope exceeds 60 LOC net change, (c) you discover a real upstream consumer of `compaction_context_window` inside `compaction.py` after the helper is removed — STOP and surface the finding before continuing.
