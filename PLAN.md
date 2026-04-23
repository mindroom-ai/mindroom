# ARCH-CLEAN-1 — Final synthesized plan (post-debate, post-AUDIT-1)

## Synthesis decision

**Pivoting to ARCH-AUDIT-1's actionable list.** Per the task: *"When ARCH-AUDIT-1 exists ... use it as the work list."* AUDIT-1 landed on gitea `arch-audit-1` branch (commit `b7517ea04`, file `docs/dev/ARCH-AUDIT-1.md`) with a debated, concrete 7-item actionable list.

The two planner agents (codex+claude) produced their own consensus of 31 underscore-ADD renames using a different rule reading ("private = no cross-leaf-directory importer"), but Bas's task quotes the rule verbatim as **"private iff nothing under `src/` (excluding tests/) imports it"** — which is AUDIT-1's stricter reading. Under that reading, the existing `memory/_X.py` and `custom_tools/_google_oauth.py` files are MIS_NAMED_PRIVATE (they have intra-package production importers), and there are 0 actionable DE_FACTO_PRIVATE_BUT_NAMED_PUBLIC items. Pivoting now to AUDIT-1's list.

The planner CRITIQUE.md files are preserved at `/srv/mindroom-worktrees/arch-clean-1-plan-{codex,claude}/CRITIQUE.md` for reference, but their consensus is **superseded** by AUDIT-1's authoritative findings.

## The actionable set (3 commits)

### Commit 1 — memory: strip underscores + add missing `_policy` visibility (atomic)

All five memory `_X.py` files have intra-package production importers (verified by grep). Per the rule, they should be public-named. The 5 renames must land in one commit so `tach check` stays green throughout — visibility lists reference each other by post-rename name.

```bash
git mv src/mindroom/memory/_file_backend.py src/mindroom/memory/file_backend.py
git mv src/mindroom/memory/_mem0_backend.py src/mindroom/memory/mem0_backend.py
git mv src/mindroom/memory/_policy.py       src/mindroom/memory/policy.py
git mv src/mindroom/memory/_prompting.py    src/mindroom/memory/prompting.py
git mv src/mindroom/memory/_shared.py       src/mindroom/memory/shared.py
```

Update production importers (siblings in `mindroom/memory/`):
- `src/mindroom/memory/__init__.py` — line 3: `from mindroom.memory._prompting import …` → `from mindroom.memory.prompting import …`
- `src/mindroom/memory/functions.py` — lines 13, 24, 33, 40, 44, 55: `from ._file_backend`, `from ._mem0_backend`, `from ._policy`, `from ._prompting`, `from ._shared` (TYPE_CHECKING guard) → drop underscores
- `src/mindroom/memory/_file_backend.py` (now `file_backend.py`) — lines 14, 24: `from ._policy`, `from ._shared` → drop underscores
- `src/mindroom/memory/_mem0_backend.py` (now `mem0_backend.py`) — lines 11, 19: `from ._policy`, `from ._shared` → drop underscores
- `src/mindroom/memory/_policy.py` (now `policy.py`) — line 9: `from ._shared` → `from .shared`

Update `tach.toml` (line numbers per origin/main `f6a73e328`):
- All `path = "mindroom.memory._X"` → `path = "mindroom.memory.X"` (5 entries)
- All `visibility = ["..._X"]` whitelist entries that reference the renamed modules → drop underscores
- `mindroom.memory` `depends_on = [...]` list → drop underscores in any entry
- **Add new `visibility = ["mindroom.memory", "mindroom.memory.functions", "mindroom.memory.file_backend", "mindroom.memory.mem0_backend"]`** to the new `mindroom.memory.policy` block (Q2 finding: `_policy` was the only memory internal without a visibility whitelist).

Run `uv run tach check` — must pass before commit.

Tests: `tests/test_memory_*.py` will need `from mindroom.memory._X` → `from mindroom.memory.X` updates. Audit by `grep -rn 'mindroom\.memory\._[a-z]' tests/` after rename.

Commit message:
```
refactor(memory): rename internal modules to public names matching importer reality

The five mindroom.memory._X internal modules (_file_backend, _mem0_backend,
_policy, _prompting, _shared) all have production importers under src/
(intra-package siblings, plus mindroom.memory facade for _prompting). Per the
naming convention "private iff nothing under src/ imports it", they are
de-facto public and should drop the leading underscore. Tach module-level
visibility=[…] whitelists already enforce the real access boundary.

Also adds the missing visibility whitelist to mindroom.memory.policy (the
only memory internal that previously lacked one).
```

### Commit 2 — custom_tools/google_oauth: strip underscore

`src/mindroom/custom_tools/_google_oauth.py` has 3 production importers (`gmail`, `google_calendar`, `google_sheets`). Same logic as memory.

```bash
git mv src/mindroom/custom_tools/_google_oauth.py src/mindroom/custom_tools/google_oauth.py
```

Update production importers:
- `src/mindroom/custom_tools/gmail.py`, `google_calendar.py`, `google_sheets.py` — `from ._google_oauth` → `from .google_oauth`
- Tests: `grep -rn '_google_oauth' tests/` and update.

`tach.toml` change: AUDIT-1 says add `[[modules]]` block is "optional/borderline scope-creep". **Decision: defer the new tach declaration** to ARCH-B-8 (cross-cutting). Just do the rename.

Commit message:
```
refactor(custom_tools): rename _google_oauth.py to google_oauth.py

Three sibling custom_tools modules (gmail, google_calendar, google_sheets)
import this file as their OAuth helper. Per the naming convention "private
iff nothing under src/ imports it", it is de-facto public and should drop
the leading underscore. Adding the tach.toml [[modules]] declaration is
deferred to ARCH-B-8 cross-cutting cleanup.
```

### Commit 3 — tach.toml: add missing `[[modules]]` block for `mcp.toolkit`

`tach.toml:1901-1907` declares `[[interfaces]] from = ["mindroom.mcp.toolkit"]` but no `[[modules]] path = "mindroom.mcp.toolkit"` block exists. The file `src/mindroom/mcp/toolkit.py` exists with 2 production importers (`mindroom.orchestrator:57`, `mindroom.mcp.registry:8`). Add the missing module declaration.

```toml
[[modules]]
path = "mindroom.mcp.toolkit"
depends_on = [
    "mindroom.tool_system.catalog",
    "mindroom.tool_system.runtime_context",
    "mindroom.tool_system.worker_routing",
]
```

Verify `depends_on` against `toolkit.py`'s actual imports before commit. Run `uv run tach check` — must pass.

Commit message:
```
chore(tach): declare orphan mindroom.mcp.toolkit module

tach.toml has an [[interfaces]] block for mindroom.mcp.toolkit but no
[[modules]] block declaring its dependencies. The orphan was missed when
mcp/ landed during ARCH-B-tools work. Add the missing module block with
depends_on derived from toolkit.py's actual imports.
```

## Out of scope (do-not-fix bucket)

Per AUDIT-1's Q4 + DevAgent's synthesis call:

1. **Deployment-entry-point modules** (`api/sandbox_runner_app.py`, `cli/main.py`) — externally referenced by `run-sandbox-runner.sh` and `pyproject.toml` console-script. Public by deployment contract.
2. **The 31 underscore-ADD renames the planners proposed.** Their reading of "private" was non-literal (leaf-directory exemption). The literal rule produces zero such renames. Both planners' analysis is preserved in their CRITIQUE.md files for future reference.
3. **Symbol-naming cleanups** (`_EventCache`, `_EventCacheWriteCoordinator`, ~26 tool_system `_X` names exposed via interfaces) — symbol naming is out of the module-naming spec. Separate refactor.
4. **The 249 undeclared `mindroom.*` modules** — explicit territory of ARCH-B-7 (api/cli/commands) and ARCH-B-8 (cross-cutting).
5. **`mindroom.tool_system` empty `depends_on`** (cosmetic only).
6. **Visibility-placement convergence (Q3 #1)** — falls out naturally as the canonical pattern is applied to future domains.

## Conflict mitigation with ARCH-B-7

ARCH-B-7 only ADDS `[[modules]]` entries for `api/cli/commands`. This plan only touches `mindroom.memory.*`, `mindroom.custom_tools.*`, and `mindroom.mcp.toolkit` — disjoint from ARCH-B-7's scope. **Zero rebase risk.**

## Validation gates (in order)

After each commit:
```bash
nix-shell shell.nix
uv run tach check                              # must say "All modules validated!"
uv run pytest tests/test_memory_*.py -x -n 0 --no-cov -q            # commit 1
# Or: tests/test_google_oauth*.py + tests/test_*calendar*.py for commit 2
# Commit 3: just tach check
```

After all 3 commits:
```bash
uv run tach check
uv run pytest tests/ -x -n 0 --no-cov -q       # full suite
uv run pre-commit run --all-files              # ruff / format / ty
python -c "from mindroom.memory import auto_flush_enabled, build_memory_prompt_parts"
python -c "from mindroom.custom_tools.google_oauth import *"
python -c "from mindroom.mcp.toolkit import *"
```

## Risks

1. **Test monkeypatch strings** — `mock.patch("mindroom.memory._X.…")` in tests will silently no-op if missed. Final grep gate: `! grep -rn 'mindroom\.memory\._\|mindroom\.custom_tools\._google_oauth' src tests` must return empty.
2. **`mindroom.memory.policy` visibility list** must include exactly the post-rename consumers (`functions`, `file_backend`, `mem0_backend`); add `mindroom.memory` only if the facade `__init__.py` references it (verify by reading the file before commit).
3. **No `--no-verify`.** AUDIT-1 explicitly warned: their audit-only commits used `--no-verify` because the venv wasn't available; ARCH-CLEAN-1 must use the standard pre-commit gate.

## Total estimated diff

- **3 commits**, ~25 files touched (5 renames + ~10 production caller files + ~10 test files + tach.toml).
- LOC delta: net **−2 lines** (one new visibility list of ~5 lines vs. zero deletions; everything else is path-string rewrites).
- Branch from `origin/main` `f6a73e328`. Push to `gitea` only.

=== FINAL PLAN ===