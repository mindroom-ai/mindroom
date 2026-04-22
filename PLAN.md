# ARCH-B-7 — Final Implementation Plan

**Status:** synthesized from `arch-b-7-plan-codex` + `arch-b-7-plan-claude` planner outputs and the cross-fed critiques (`/tmp/ARCH-B-7-{CODEX,CLAUDE}-{PLAN,CRITIQUE}.md`).
**Branch:** `arch-b-7` off `origin/main@5dd265bea`.
**Mission:** declare every undeclared module under `mindroom.api.*`, `mindroom.cli.*`, `mindroom.commands.*` in `tach.toml` as a **consumer-only `[[modules]]`** entry with a realistic `depends_on` list. After this PR, `tach check` mechanically refuses any new file in api/cli/commands that imports outside its declared `depends_on` allowlist.

## 0. Method note

Each planner ran independently against `origin/main` HEAD `5dd265bea`. They cross-fed plans and produced critiques. Both critiques explicitly recommend the same direction on every divergence point. This synthesis lists for each topic which planner won and why.

**Module count:** 23 leaf modules + 3 bare-package markers (`api`, `cli`, `commands`) = **26**. Both planners agree.

## 1. Per-module `depends_on` (final)

Synthesizer rule: **list the modules the file actually imports** (sub-module spelling, not invented facades), and **list TYPE_CHECKING deps unconditionally**. No source-file rewrites in this PR. No new `[[interfaces]]` blocks.

### 1a. Bare-package markers

```toml
[[modules]]
path = "mindroom.api"
depends_on = []

[[modules]]
path = "mindroom.cli"
depends_on = []

[[modules]]
path = "mindroom.commands"
depends_on = []
```

Precedent: `mindroom.tool_system` is declared bare at `tach.toml:1054`. Each `__init__.py` is docstring-only.

### 1b. `mindroom.api.*` (15 leaf modules)

```toml
[[modules]]
path = "mindroom.api.config_lifecycle"
depends_on = [
    "mindroom.config.main",
    "mindroom.constants",
    "mindroom.file_watcher",
    "mindroom.logging_config",
]

[[modules]]
path = "mindroom.api.google_integration"
depends_on = [
    "mindroom.api.config_lifecycle",
    "mindroom.api.credentials",
    "mindroom.api.main",
    "mindroom.constants",
    "mindroom.credentials",
    "mindroom.tool_system.dependencies",
    "mindroom.tool_system.worker_routing",
]

[[modules]]
path = "mindroom.api.google_tools_helper"
depends_on = []

[[modules]]
path = "mindroom.api.homeassistant_integration"
depends_on = [
    "mindroom.api.credentials",
    "mindroom.api.integrations",
    "mindroom.api.main",
]

[[modules]]
path = "mindroom.api.integrations"
depends_on = [
    "mindroom.api.credentials",
    "mindroom.api.main",
    "mindroom.constants",
    "mindroom.tool_system.dependencies",
    "mindroom.tool_system.worker_routing",
]

[[modules]]
path = "mindroom.api.knowledge"
depends_on = [
    "mindroom.api.config_lifecycle",
    "mindroom.config.main",
    "mindroom.constants",
    "mindroom.knowledge",
]

[[modules]]
path = "mindroom.api.sandbox_exec"
depends_on = [
    "mindroom.constants",
    "mindroom.tool_system.worker_routing",
    "mindroom.workers.backends.local",  # TYPE_CHECKING (LocalWorkerStatePaths) — see §2 V4
]

[[modules]]
path = "mindroom.api.sandbox_protocol"
depends_on = []

[[modules]]
path = "mindroom.api.sandbox_runner"
depends_on = [
    "mindroom.api.sandbox_exec",
    "mindroom.api.sandbox_protocol",
    "mindroom.api.sandbox_worker_prep",
    "mindroom.config.main",
    "mindroom.constants",
    "mindroom.credentials",
    "mindroom.logging_config",
    "mindroom.tool_system",                  # function-local: `from mindroom.tool_system import plugin_imports` — see §2 V1
    "mindroom.tool_system.catalog",
    "mindroom.tool_system.plugin_imports",   # see §2 V1 — likely needs temp Tach exception
    "mindroom.tool_system.plugins",
    "mindroom.tool_system.sandbox_proxy",
    "mindroom.tool_system.worker_routing",
    "mindroom.workers.backends.local",       # see §2 V5
    "mindroom.workers.models",               # TYPE_CHECKING (WorkerHandle)
]

[[modules]]
path = "mindroom.api.sandbox_runner_app"
depends_on = ["mindroom.api.sandbox_runner"]

[[modules]]
path = "mindroom.api.sandbox_worker_prep"
depends_on = [
    "mindroom.api.sandbox_exec",
    "mindroom.constants",
    "mindroom.logging_config",
    "mindroom.tool_system.sandbox_proxy",
    "mindroom.tool_system.worker_routing",
    "mindroom.workers.backend",              # WorkerBackendError — see §2 V6
    "mindroom.workers.backends.local",       # 5 helpers — see §2 V6
    "mindroom.workers.models",               # WorkerHandle, WorkerSpec
]

[[modules]]
path = "mindroom.api.schedules"
depends_on = [
    "mindroom.api.config_lifecycle",
    "mindroom.api.main",
    "mindroom.config.main",
    "mindroom.constants",
    "mindroom.logging_config",
    "mindroom.matrix.rooms",
    "mindroom.matrix.users",
    "mindroom.scheduling",
]

[[modules]]
path = "mindroom.api.skills"
depends_on = [
    "mindroom.constants",
    "mindroom.tool_system.skills",
]

[[modules]]
path = "mindroom.api.workers"
depends_on = [
    "mindroom.api.config_lifecycle",
    "mindroom.tool_system.sandbox_proxy",
    "mindroom.workers.manager",   # TYPE_CHECKING (WorkerManager)
    "mindroom.workers.models",    # TYPE_CHECKING (WorkerHandle)
    "mindroom.workers.runtime",
]
```

### 1c. `mindroom.cli.*` (6 leaf modules)

```toml
[[modules]]
path = "mindroom.cli.banner"
depends_on = []

[[modules]]
path = "mindroom.cli.config"
depends_on = [
    "mindroom.config.main",
    "mindroom.constants",
    "mindroom.credentials_sync",
    "mindroom.tool_system.worker_routing",
    "mindroom.workspaces",
]

[[modules]]
path = "mindroom.cli.connect"
depends_on = ["mindroom.constants"]

[[modules]]
path = "mindroom.cli.doctor"
depends_on = [
    "mindroom.cli.config",
    "mindroom.config.main",
    "mindroom.config.models",   # TYPE_CHECKING (ModelConfig)
    "mindroom.constants",
    "mindroom.embeddings",
    "mindroom.matrix.health",
]

[[modules]]
path = "mindroom.cli.local_stack"
depends_on = [
    "mindroom.cli.config",
    "mindroom.constants",
    "mindroom.matrix.health",
]

[[modules]]
path = "mindroom.cli.main"
depends_on = [
    "mindroom.avatar_generation",   # function-local
    "mindroom.cli.banner",
    "mindroom.cli.config",
    "mindroom.cli.connect",
    "mindroom.cli.doctor",
    "mindroom.cli.local_stack",
    "mindroom.config.main",
    "mindroom.constants",
    "mindroom.error_handling",
    "mindroom.frontend_assets",
    "mindroom.orchestrator",        # function-local
]
```

### 1d. `mindroom.commands.*` (3 leaf modules)

```toml
[[modules]]
path = "mindroom.commands.config_commands"
depends_on = [
    "mindroom.api.config_lifecycle",
    "mindroom.config.main",
    "mindroom.constants",
    "mindroom.logging_config",
]

[[modules]]
path = "mindroom.commands.config_confirmation"
depends_on = [
    "mindroom.bot",
    "mindroom.commands.config_commands",
    "mindroom.logging_config",
]

[[modules]]
path = "mindroom.commands.parsing"
depends_on = [
    "mindroom.constants",
    "mindroom.logging_config",
]
```

### 1e. Required edits to existing `[[modules]]` entries (mechanical, in scope)

Once siblings are declared, parents that import them must list them. Add only what their files already import.

**`mindroom.api.main`** — add 8 missing sibling api edges:
- `mindroom.api.config_lifecycle`
- `mindroom.api.google_integration`
- `mindroom.api.homeassistant_integration`
- `mindroom.api.integrations`
- `mindroom.api.knowledge`
- `mindroom.api.schedules`
- `mindroom.api.skills`
- `mindroom.api.workers`

**`mindroom.api.matrix_operations`** — add `mindroom.api.config_lifecycle` (line 10) and `mindroom.logging_config` (line 11).

**`mindroom.api.openai_compat`** — add `mindroom.api.config_lifecycle` (line 31) and `mindroom.logging_config` (line 43).

**`mindroom.api.tools`** — add `mindroom.api.config_lifecycle` and `mindroom.api.google_tools_helper`.

**`mindroom.commands.handler`** — full amendment (Claude-critique §3 confirmed Codex undercount). Add the 7 truly missing top-level imports:
- `mindroom.authorization`
- `mindroom.commands.config_commands`
- `mindroom.commands.config_confirmation`
- `mindroom.commands.parsing`
- `mindroom.handled_turns`
- `mindroom.logging_config`
- `mindroom.thread_utils`

(Plus any TYPE_CHECKING-only deps Tach surfaces during the green-loop pass — add eagerly when they fire, do not pre-emptively widen with speculative entries.)

## 2. Production-code violations and dispositions

The implementer must NOT auto-fix these. They are surfaced for Bas; surface as a single batched interactive Q before merge if any is blocking.

### V1 — `sandbox_runner` reaches into `tool_system.plugin_imports._resolve_plugin_root` (BLOCKING)
- **File:line:** `src/mindroom/api/sandbox_runner.py:183` (`from mindroom.tool_system import plugin_imports`), called at `:193` as `plugin_imports._resolve_plugin_root(...)`.
- **Why blocking:** `tach.toml:1665-1671` declares `mindroom.tool_system.plugin_imports` `exclusive = true` with visibility restricted to `["mindroom.tool_system.metadata", "mindroom.tool_system.plugins", "mindroom.tool_system.registry_state"]`. `mindroom.api.sandbox_runner` is not in that list. Adding `mindroom.tool_system.plugin_imports` to the consumer's `depends_on` will NOT satisfy Tach because the interface is exclusive.
- **Disposition:** `NEEDS BAS DECISION`. Options:
  - **A) Widen** — expose a public `resolve_plugin_root(plugin_path, runtime_paths)` on `mindroom.tool_system.plugins` (or `tool_system.catalog`) and switch the caller. Recommended by Claude.
  - **B) Move** — hoist `_config_with_available_plugins` into `mindroom.tool_system.plugins` so `_resolve_plugin_root` stays private to its domain.
  - **C) Temporary Tach exception** — if Bas wants ARCH-B-7 to land cleanly without source changes, add a narrowly-scoped allowance at `mindroom.tool_system.plugin_imports`'s `visibility` list (or via per-consumer override) with a `# TODO ARCH-B-7-followup` comment that points here.
- **Recommended path for THIS PR:** option C as a temporary tach exception, with the underlying choice between A/B raised as the post-merge follow-up. **Implementer must ask before touching the visibility list.**

### V2 — `sandbox_runner` imports private `_normalized_config_data` from `config.main`
- **File:line:** `src/mindroom/api/sandbox_runner.py:27`.
- **Why not blocking:** `mindroom.config.main` has no `[[interfaces]]` declared, so Tach doesn't enforce the underscore convention here.
- **Disposition:** stylistic hack, **out of scope** for this PR. Note for future ARCH-B-config.

### V3 — `commands.handler` imports `TOOL_METADATA, ToolMetadata` from `tool_system.metadata`
- **File:line:** `src/mindroom/commands/handler.py:28`.
- **Why not blocking:** already declared in `tach.toml:926`, already passes today.
- **Disposition:** **out of scope.** Hard Rule #6 — pre-existing, not introduced by this work, do not fold in. Mentioned only so reviewers don't flag it as new.

### V4 — `api.sandbox_exec` TYPE_CHECKING dep on `workers.backends.local`
- **File:line:** `src/mindroom/api/sandbox_exec.py:17`.
- **Disposition:** **declare honestly** in §1b `sandbox_exec` `depends_on`. This is what the file actually imports; the right answer is to list it, not to refactor. The cluster-relocation question is a separate ARCH-B-9 item (§3 Q1).

### V5 — `api.sandbox_runner` runtime import `from mindroom.workers.backends.local import get_local_worker_manager`
- **File:line:** `src/mindroom/api/sandbox_runner.py:53`.
- **Disposition:** **declare honestly** in §1b `sandbox_runner` `depends_on`. No source change. Same cluster-relocation concern as V4.

### V6 — `api.sandbox_worker_prep` imports 5 helpers from `workers.backends.local`
- **File:line:** `src/mindroom/api/sandbox_worker_prep.py:20-25`.
- **Disposition:** **declare honestly** in §1b `sandbox_worker_prep` `depends_on`. Same as V4/V5.

**Net per-violation actions for the implementer:** V1 → likely temporary Tach visibility exception (after Bas confirms); V2/V3 → leave alone; V4/V5/V6 → already covered by §1b dep lists, no source changes.

## 3. Module-placement open questions (FLAG-ONLY — NOT in this PR)

### Q1 — Sandbox cluster (`api.sandbox_*`, 5 modules) → `mindroom.workers.sandbox.*`?

The cluster looks more like worker mechanics than API surface (only `sandbox_runner_app` exposes a FastAPI sidecar). Three of five reach into `workers.backends.local` directly. Plausible ARCH-B-9 candidate. **Don't move now; flag for Bas as Q1.**

### Q2 — `api.openai_compat` (1551 LOC) as its own top-level domain?

ARCH-A §6 calls it "effectively a second API subsystem." Imports practically every runtime kernel module. Plausible split. **Don't touch now; flag for Bas as Q2.**

### Q3 — CLI as ops/bootstrap domain (`mindroom.ops.cli`)?

Cosmetic rename with no enforcement gain unless a sibling ops surface emerges. **Don't move now; flag for Bas as Q3 — recommended NO unless future need.**

## 4. Implementation phasing

**Single Codex implementer on `arch-b-7`** via tmux session `arch-b-7`, kept alive for fix cycles.

Likely phasing:
- **Commit 1:** all 26 new `[[modules]]` entries + the 6 amendments to existing parents (§1a-§1e).
- **Commit 2 (only if Tach trips on V1 and Bas approved option C):** narrowly-scoped temporary Tach visibility exception for `mindroom.tool_system.plugin_imports` ↔ `mindroom.api.sandbox_runner`, with `# TODO ARCH-B-7-followup` comment.

Greens loop: run `tach check`, add any missing edge Tach surfaces, repeat. **Do NOT** widen by stuffing speculative deps; only add what Tach demands or what `1e` parent files actually import today.

## 5. Risks and pre-empts

1. **Tach trips on V1 sandbox_runner → plugin_imports private symbol** — the only structural risk. Mitigation: option C temporary visibility exception (pending Bas), or surface to Bas before any source change.
2. **`tach check` rejects on a TYPE_CHECKING edge** — list it. Tach in this repo enforces TYPE_CHECKING imports (`ignore_type_checking_imports` is unset). Both planners' implicit assumption that "they don't count" is wrong; the Codex-style "list everything" discipline wins here.
3. **`tach check` rejects on a missing edge in parents** — covered by §1e's amendments. If a new edge surfaces, add only the named target.
4. **`pre-commit run --all-files` reorders `tach.toml`** — accept the autofix loop, irrelevant.
5. **Cycle complaint** — `api.google_integration` / `api.homeassistant_integration` / `api.integrations` / `api.schedules` function-local-import `api.main`. `tach.toml` global config does not set `forbid_circular_dependencies`, so cycles tolerated. If Tach trips, declare the function-local edge; do not refactor.
6. **Tests break** — `tach.toml` `exclude` covers `**/tests`, so test code is not governed. Zero test impact expected.

## 6. Out-of-scope (Hard Rule #6)

- No internal refactor of `api/main.py`, `openai_compat.py`, `cli/config.py`, or any other module.
- No splitting any api/cli/commands module.
- No sandbox cluster relocation (Q1).
- No CLI relocation (Q3).
- No openai_compat promotion (Q2).
- No fold-in of the V3 `commands.handler` → `tool_system.catalog` swap.
- No fold-in of the V2 `_normalized_config_data` cleanup.
- No new `[[interfaces]]`, `expose =`, or `visibility =` for any of the 26 new modules.
- No facade-getter wrappers / shims to satisfy Tach. **Surface to Bas instead.**
- No silent module moves.
- No defensive hardening, docstring polish, or unrelated cleanup.
- No widening of `tool_system.plugin_imports` visibility WITHOUT explicit Bas approval first (V1 option C is the lowest-risk path but still requires Bas's go-ahead).

## 7. Live-test gates

- `nix-shell --run 'uv run tach check'` returns 0
- `nix-shell --run 'uv run pytest tests/ -x -n 0 --no-cov -v'` for the relevant slice (or full slice — this is config-only)
- `pre-commit run --all-files` returns 0 (after autofix loop)
- Smoke import: `uv run python -c "import mindroom.api, mindroom.cli, mindroom.commands; from mindroom.api import main, openai_compat, schedules, knowledge, integrations; from mindroom.cli import main as cli_main; from mindroom.commands import handler"` returns 0

## 8. Net divergence resolution table (for the record)

| # | Topic | Choice | Loser |
|---|---|---|---|
| 1 | `workers.*` granularity in api modules' deps | Sub-module spelling (`workers.backend`, `workers.backends.local`, `workers.manager`, `workers.models`, `workers.runtime`); NO source rewrites | Codex's invented `mindroom.workers` facade |
| 2 | V1 `sandbox_runner → plugin_imports._resolve_plugin_root` | Flag as `NEEDS BAS DECISION`; temp Tach exception fallback if Bas approves option C | Codex blandly listing the dep without flagging exclusive-interface mismatch |
| 3 | `commands.handler` parent amendment | Add all 7 missing top-level deps (authorization, handled_turns, logging_config, thread_utils, + 3 new siblings) | Codex's incomplete +3-only amendment |
| 4 | `api.workers` granularity | Sub-modules (`workers.manager`, `workers.models`, `workers.runtime`) | Codex's bare `mindroom.workers` |
| 5 | V3 `commands.handler → tool_system.metadata` | Leave for follow-up; do not fold in | Codex's "while we're here" hedge |
| 6 | Bare `mindroom.api/cli/commands` markers | Both agreed: declare with empty deps | — |
| 7 | TYPE_CHECKING in deps | List unconditionally; drop "optionally omit" hedges | Claude's hedge in §1e |
| 8 | `sandbox_exec` TYPE_CHECKING `workers.backends.local` dep | Declared explicitly per Codex's table | Claude's omission in §1b |
