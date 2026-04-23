# ARCH-B-8a — Final Synthesized Implementation Plan

**Branch:** `arch-b-8a` (off `origin/main@755e48ef8`)
**Source plans:**
- `arch-b-8a-plan-codex` @ `e47844d06` (`IMPLEMENTATION_PLAN.md`)
- `arch-b-8a-plan-claude` @ `270ff3617` (`IMPLEMENTATION_PLAN.md`)
**Critiques:**
- `arch-b-8a-plan-codex` @ `02566cbfc` (`CRITIQUE.md`)
- `arch-b-8a-plan-claude` @ `7c4e8a4ae` (`CRITIQUE.md`)

This file is committed as the FIRST commit on `arch-b-8a` and **deleted before push to gitea** (per ARCH-B precedent: planning artifacts never go into the PR diff).

---

## 1. Synthesis: where critiques converged

Both planners independently reached the same conclusions on **5 of 8** divergence points. Where they disagreed, evidence (real `import` grep on origin/main) breaks the tie.

| # | Topic | Resolution | Source |
|---|---|---|---|
| C1 | Keep `mindroom.hooks.context → mindroom.hooks.matrix_admin` runtime edge | KEEP — verified at `src/mindroom/hooks/context.py:197-199` (`from .matrix_admin import build_hook_matrix_admin` inside the function body) | both critiques agree |
| C2 | Lazy `build_hook_matrix_admin` wrapper in `hooks/__init__.py:130-137` | DROP wrapper, direct re-export `from .matrix_admin import build_hook_matrix_admin` — `nio` already loaded transitively elsewhere; no real deferral benefit | both critiques agree |
| C3 | Test-file migrations to facade | SKIP — tests are Tach-excluded (`tach.toml exclude = […, "**/tests", …]`); migrating tests forces facade to expose private symbols, which is the wrong direction | both critiques agree |
| C4 | `visibility = […]` on the two new facade `[[interfaces]]` blocks (`mindroom.history`, `mindroom.hooks`) | OMIT — broad cross-domain consumer base (~9-15 callers each); allowlist would be brittle | both critiques agree |
| C5 | Private/underscored symbols in facade `expose` (`_to_k`, `_eligible_hooks`, `_emit_compaction_hook`, `_INTERRUPTED_RESPONSE_MARKER`, `HistoryScopeState`, `InterruptedReplaySnapshot`, `build_interrupted_replay_*`, `render_interrupted_replay_content`) | DROP from expose — production grep shows zero cross-module callers (intra-package only); the only consumers are tests (Tach-excluded) | both critiques agree |
| C6 | `mindroom.matrix.identity` / `mindroom.matrix.mentions` in `hooks.sender` and `hooks.matrix_admin` `depends_on` | ADD — verified runtime imports at `hooks/sender.py:9-10` (`from mindroom.matrix.identity import MatrixID` + `from mindroom.matrix.mentions import format_message_with_mentions`) and `hooks/matrix_admin.py:11` (`from mindroom.matrix.identity import extract_server_name_from_homeserver`); convention in tach.toml is to list these even when target module isn't itself declared (see `tach.toml:67,470,607,662,976,1003,1371,1373,1382,1468`) | Claude critique D-6 wins; Codex's D-4 was inconsistent with the existing repo convention |
| C7 | Per-submodule `visibility = […]` allowlists for every `mindroom.history.*` and `mindroom.hooks.*` submodule | OMIT — beyond "declare modules + add facade interfaces" mission. ARCH-CLEAN-1 owns visibility tightening; do not preempt it. Critique disagreement breaks toward SCOPE-DISCIPLINE per Hard Rule #6 | Codex critique D-5 wins on Hard Rule #6 grounds |
| C8 | Expose `PreparedScopeHistory`, `ResolvedReplayPlan`, `HookCallback`, `HookRegistryPlugin` through facades | EXPOSE — these have legitimate cross-domain TYPE_CHECKING consumers at `execution_preparation.py:47-48`, `agents.py:70`, `tool_system/plugins.py:34`. They're public Protocols/dataclasses used in type hints across the codebase. Exposing them is exactly the facade's job | Claude critique §6 wins; Codex's D-3 conflated these public types with the underscored helpers from C5 |

## 2. Real bugs fixed by synthesis

- **B-1:** Codex's plan dropped `mindroom.hooks.context → mindroom.hooks.matrix_admin` — that's a real runtime import at `context.py:197-199`. Restored (per C1).
- **B-2:** Claude's plan kept the lazy wrapper — unnecessary indirection. Removed (per C2).
- **B-3:** Both plans had `_INTERRUPTED_RESPONSE_MARKER` and `HistoryScopeState` in facade expose despite zero cross-module callers. Stripped (per C5).
- **B-4:** Codex's plan omitted `mindroom.matrix.identity/mentions` from `hooks.sender` and `hooks.matrix_admin` deps despite real top-level runtime imports. Added (per C6).

## 3. Final exposed surfaces

### `mindroom.history` `[[interfaces]] expose=[…]`

Production-callers grep against `from mindroom.history` outside `mindroom/history/` shows the union of:

```
HistoryEntry
HistoryStore
HookEnrichmentStore
build_compaction_summary
compact_thread
get_history_runtime_state
get_history_store
get_message_history_summary
get_thread_history
get_token_budget_for_agent
maybe_compact_thread
prepare_history_runtime
prepare_scope_history
record_turn
should_compact_thread
PreparedScopeHistory       # cross-domain TYPE_CHECKING consumers (per C8)
ResolvedReplayPlan         # cross-domain TYPE_CHECKING consumers (per C8)
```

(Implementer: derive the EXACT list from Codex/Claude plan §3 / §6 facades, then verify with a grep `from mindroom.history.* import` across `src/` excluding `mindroom/history/`. Add anything missing, drop anything not actually consumed.)

### `mindroom.hooks` `[[interfaces]] expose=[…]`

Production-callers grep against `from mindroom.hooks` outside `mindroom/hooks/`:

```
EVENT_*               # event constants used by registrants
EnrichmentItem
HookContext
HookSenderContext
RegisteredHook
build_hook_context
build_hook_matrix_admin    # direct re-export, NOT a wrapper (per C2)
build_hook_sender_context
default_timeout_ms_for_event
emit_event
get_hook_metadata
get_hook_state
hook                       # decorator
hook_state_lock
register_hook
register_hooks_from_config
HookCallback               # cross-domain TYPE_CHECKING consumers (per C8)
HookRegistryPlugin         # cross-domain TYPE_CHECKING consumers (per C8)
```

(Same derivation rule as above. Verify against `__all__` already in `src/mindroom/hooks/__init__.py:1-130` — most of this is already there.)

## 4. Per-module `[[modules]]` declarations

The implementer follows **Claude's plan §1 + §2** verbatim for the 13 new module blocks, MINUS the per-submodule `visibility = […]` allowlists (per C7), PLUS:
- Add `mindroom.matrix.identity` to `mindroom.hooks.matrix_admin` depends_on (per C6)
- Add `mindroom.matrix.identity` and `mindroom.matrix.mentions` to `mindroom.hooks.sender` depends_on (per C6)

Modules to declare (13 total):

**History (5):**
1. `mindroom.history` (facade) — depends_on = union of internal submodules used by re-exports
2. `mindroom.history.compaction` — depends_on derived from real imports in `compaction.py` (NO TYPE_CHECKING)
3. `mindroom.history.policy` — `["mindroom.history.compaction", "mindroom.history.types", "mindroom.token_budget"]` (verify)
4. `mindroom.history.storage` — `["mindroom.constants", "mindroom.history.types"]` (verify)
5. `mindroom.history.types` — depends_on derived from real imports

**Hooks (8 — note `__init__` is the 8th, brief said 7):**
1. `mindroom.hooks` (facade) — depends_on = union of submodules
2. `mindroom.hooks.decorators` — `["mindroom.hooks.types"]`
3. `mindroom.hooks.enrichment` — `[]` (only stdlib)
4. `mindroom.hooks.execution` — `["mindroom.hooks.context", "mindroom.hooks.types", "mindroom.logging_config"]`
5. `mindroom.hooks.ingress` — `["mindroom.hooks.types"]`
6. `mindroom.hooks.registry` — `["mindroom.hooks.decorators", "mindroom.hooks.types", "mindroom.logging_config"]`
7. `mindroom.hooks.state` — `[]` (only stdlib + third-party `nio`)
8. `mindroom.hooks.types` — depends_on derived from real imports

**Plus 3 amendments to existing blocks:**
- `mindroom.hooks.matrix_admin` (currently `["mindroom.matrix.client_room_admin"]`) → add `mindroom.matrix.identity`
- `mindroom.hooks.sender` (currently `["mindroom.matrix.client_delivery"]`) → add `mindroom.matrix.identity`, `mindroom.matrix.mentions`
- `mindroom.hooks.context` (currently declared) → drop phantom TYPE_CHECKING `mindroom.matrix.cache`; verify `mindroom.hooks.matrix_admin` is listed (it should be, per C1)

**Hard rule on TYPE_CHECKING imports:** ARCH-B-7 R3 lesson — DO NOT include TYPE_CHECKING-only imports in `depends_on`. Tach treats them as "unused" under `--exact` and they bless misleading cycles.

## 5. Caller migrations

Follow Claude plan §5 (the longest list). For every cross-domain `from mindroom.history.<sub> import …` and `from mindroom.hooks.<sub> import …` in `src/` that imports a symbol now exposed by the facade, rewrite to `from mindroom.history import …` / `from mindroom.hooks import …`.

**Concrete callers to migrate (verify with `grep -rn 'from mindroom.history\|from mindroom.hooks' src/ | grep -v '/mindroom/history/\|/mindroom/hooks/'`):**
- `src/mindroom/ai.py`
- `src/mindroom/api/openai_compat.py`
- `src/mindroom/bot.py`
- `src/mindroom/config/main.py`
- `src/mindroom/conversation_state_writer.py`
- `src/mindroom/custom_tools/compact_context.py`
- `src/mindroom/delivery_gateway.py`
- `src/mindroom/edit_regenerator.py`
- `src/mindroom/execution_preparation.py`
- `src/mindroom/orchestrator.py`
- `src/mindroom/response_runner.py`
- `src/mindroom/scheduling.py`
- `src/mindroom/teams.py`
- `src/mindroom/tool_system/plugins.py`
- `src/mindroom/tool_system/runtime_context.py`
- `src/mindroom/tool_system/tool_hooks.py`
- `src/mindroom/turn_controller.py`
- `src/mindroom/turn_policy.py`

(Implementer must NOT migrate test files. Per C3, tests keep their direct-submodule imports.)

## 6. Facade content (the two `__init__.py` files)

### `src/mindroom/history/__init__.py`

Currently 19-LOC stub. Becomes the facade with `__all__` listing exactly the symbols in §3 above, one per line, with `from .X import Y` re-exports. Mirror the layout of `src/mindroom/knowledge/__init__.py` (PR #657 precedent).

### `src/mindroom/hooks/__init__.py`

Currently 137-LOC `__all__`-based facade. Already mostly the right shape. Two edits:
1. Drop the lazy `build_hook_matrix_admin` wrapper at lines ~130-137; replace with top-level `from .matrix_admin import build_hook_matrix_admin`.
2. Add any C8 entries (`HookCallback`, `HookRegistryPlugin`) to `__all__` and re-export.

## 7. Validation gates

In order, all under `nix-shell --run`:
1. `uv run tach check` → "All modules validated!" — HARD GATE
2. `uv run pytest -x -n 0 --no-cov` (full suite) — HARD GATE
3. `pre-commit run --all-files` — HARD GATE
4. `uv run python -c 'from mindroom.history import *; from mindroom.hooks import *; print("facades importable")'`
5. `git diff --stat origin/main...arch-b-8a` — verify scope (~30-50 files, mostly small import changes; NO PLAN/CRITIQUE/FINAL-PLAN files in the diff after pre-push cleanup)

## 8. Out of scope (anti-scope-creep — Hard Rule #6)

- Splitting `compaction.py` (1343 LOC stays whole — original Apr-15 spec)
- Splitting `runtime.py` (1182 LOC) or `context.py` (797 LOC)
- Renaming any module to `_underscore` (ARCH-CLEAN-1's lane)
- Per-submodule `visibility` allowlists (ARCH-CLEAN-1's lane)
- Test-file import migrations
- Any docstring/typing polish on untouched modules
- Any "while we're here" cleanup in history/ or hooks/ internals

## 9. Open questions for Bas

NONE. All open questions raised by either planner were resolved by the cross-critique evidence. Going fully autonomous.

## 10. Pre-push cleanup checklist (HARD)

Before `git push gitea arch-b-8a`:
1. `git rm FINAL-PLAN.md` (this file)
2. Verify no `IMPLEMENTATION_PLAN.md` / `PLAN.md` / `CRITIQUE.md` / `PLAN-OTHER.md` exist in the worktree
3. `git diff origin/main...HEAD --stat` — should be `tach.toml` + `src/mindroom/history/__init__.py` + `src/mindroom/hooks/__init__.py` + ~15-20 caller files. Total ~30-50 files, all small.
4. If diff explodes — ABORT, investigate.

---

**This plan is the FIRST commit on `arch-b-8a`. Implementer follows it verbatim. Pre-commit cleanup deletes this file before squash-merge.**
