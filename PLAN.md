# PLAN — ISSUE-246: compaction "token estimate" is a byte count; make logs truthful

Synthesized from two independent plans (codex + claude) and their cross-critiques.
Both planners independently converged on the core call: **keep UTF-8 bytes as the estimator, add NO divisor, rename the log fields to be truthful.** Any divisor > 1 violates the never-undercount requirement (CJK/emoji/byte-fallback content drives the real bytes-per-token ratio toward 1.0).

## Verbatim user report (from ISSUE-246.md)

See `skills/mindroom-dev/references/reports/ISSUE-246.md` — the `estimated_input_tokens` field in compaction chunk logs is actually `len(payload.encode("utf-8"))` (a byte count used as a conservative token upper bound), which makes the logs read as if token estimates are ~3-4x too high.

## Hard requirements

1. Structured log fields must be truthful: a byte count must never be labeled `estimated_input_tokens`.
2. The sizing estimate used for chunk selection must NEVER undercount actual tokens (safety property — undercounting causes oversized summary calls that fail or truncate).
3. Zero numeric behavior change for Claude/known-tiktoken models. The one deliberate behavior change (see Commit 2) must be isolated and droppable.
4. Minimize merge surface in `_generate_compaction_summary_with_retry` (`compaction.py` ~520-605) — ISSUE-243 review rounds keep rewriting that hunk. **Log-kwargs-only edits there; NO local variable renames.**

## Coordination with ISSUE-243 (settled)

Verified: `git diff HEAD origin/issue-243 -- src/mindroom/history/compaction.py src/mindroom/history/summary_call.py src/mindroom/token_budget.py` is **empty** — the 243 delta is already absorbed into this base. Proceed now; do NOT wait for 243. The only residual risk is future 243 review rounds, which argues for a small merge surface, not for blocking.

## Design

### API (token_budget.py)

Replace `estimate_compaction_input_tokens(value, *, model_id=None, conservative_fallback=False)` with:

```python
CompactionEstimateKind = Literal[
    "model_tiktoken_tokens",        # known encoding via tiktoken.encoding_for_model()
    "o200k_base_tokens",            # surrogate encoding (vertex guard only after Commit 2)
    "utf8_bytes_token_upper_bound", # conservative byte bound
]

def compaction_payload_token_upper_bound(value: str, *, model_id: str | None) -> int: ...
def compaction_estimate_kind(model_id: str | None) -> CompactionEstimateKind: ...
```

- Known tiktoken encoding → exact tiktoken count (unchanged numeric behavior).
- Otherwise → `len(value.encode("utf-8"))` byte bound.
- The kind resolver is the SINGLE source of truth for the branch condition; the bound function branches on the resolver's result (prevents drift — codex critique 2C).
- Delete the `conservative_fallback` parameter entirely (no-backward-compat rule; Bas is the only user).
- Drop the `as_anthropic_claude` import/gate from `compaction.py` (~line 21, 353): encoding-absence is the fallback criterion, provider-agnostic and self-maintaining for future model IDs.

### Vertex guard (MUST NOT change numerically)

`vertex_claude_compat.py:281` calls the estimator with no model_id — today that IS the o200k path, and pointing it at a byte bound would inflate request estimates ~3.3x and change when exact counting/trimming triggers (claude critique 2b). Fix: add an honestly-named `approximate_o200k_tokens(value)` in `token_budget.py`, point `vertex_claude_compat.py` at it, update the patch targets in `tests/test_vertex_claude_context_guard.py` (~142, 167, 203). Guard behavior numerically unchanged.

### Log schema (exact old → new mapping)

| Old field | New field |
| --- | --- |
| `estimated_input_tokens` | `summary_input_estimate` |
| (new) | `summary_input_estimate_kind` (the Literal above) |
| `summary_input_budget` | `summary_input_budget_tokens` |

- Apply via ONE small `_sizing_log_fields(...)` helper to the three chunk events (`compaction.py` ~532-542, 551-563, 594-605).
- The no-run-fit warning (`compaction.py:379-385`) renames its budget field only — no estimate kind there (no payload was produced).
- Do NOT emit a duplicate raw-bytes field: when kind is `utf8_bytes_token_upper_bound`, the estimate IS the byte count.
- NOTE: `runtime.py`'s `Compaction completed` log is chars/4 token-domain, NOT bytes — leave it alone (claude Phase-0 finding; the issue text overcounts affected logs).

### Commit structure (two commits, second droppable)

1. **Pure relabeling, zero numeric change anywhere:** log schema rename, API rename/split, vertex `approximate_o200k_tokens` helper, kind resolver, tests.
2. **The one deliberate behavior change:** unknown non-Claude compaction models (Gemini, local OpenAI-compatible) switch from o200k surrogate to byte bound. This makes them SAFE (the repo's own regression note at `tests/test_compaction_invariants.py:~1374` documents tiktoken undercounting by 1.63x) but ~3-4x more conservative (more chunks/summary calls). Call it out in the commit body; Bas can drop it if he wants the PR strictly cosmetic.

## Tests

- **Never-undercount regression (the strong form, codex critique 2E):** for the byte-bound branch, assert EXACT equality with `len(value.encode("utf-8"))`, parameterized over ASCII, CJK, emoji, combining marks, zero-width joiners, mixed text. Fails immediately if anyone introduces a divisor. (Do NOT "prove" the universal property by comparing against one surrogate tokenizer.)
- Keep the known-model (`gpt-4o`) tiktoken-count test unchanged in place (`test_compaction_invariants.py` ~1354-1369 is the ONLY in-place edit there — new coverage goes in new files to dodge 243 churn).
- **New files:** `tests/test_token_budget.py` (API + kind resolver + vertex helper), `tests/test_compaction_sizing_logs.py` (all THREE chunk events + the no-run-fit warning, via `structlog.testing.capture_logs` — pattern at `tests/test_history_summary_call.py:14,382`). Assert old field names are ABSENT.
- Vertex guard tests: update patch targets, assert numeric thresholds unchanged.
- Non-ASCII chunk-selection integration parameterization: dense CJK/emoji payload, proving selection never applies a prose-derived ratio.

## Explicitly rejected (from the debate)

- Any `bytes/N` divisor heuristic — violates requirement 2.
- Provider `count_tokens` as sync estimator — Anthropic documents it as an estimate (can't prove the bound), Bedrock client has no counting support, and it adds a network call to a hot path.
- Renaming retry-loop locals (`estimated_input_tokens`, `rebuilt_input_tokens` at ~530-585) — textual-conflict churn in exactly the hunk 243 keeps rewriting, zero runtime benefit.
- Two-dataclass sizing API (`CompactionInputSizing`/`CompactionInputMeasurement`) — over-modeled; a kind resolver + integer function covers the schema.
- Waiting for ISSUE-243 to land — its delta is already absorbed (verified empty diff).

## Gates

All three: `uv run pytest` (full suite), `pre-commit run --all-files`, `uv run tach check`.
