# ISSUE-213 — FINAL Implementation Plan

**Synthesized 2026-05-07 by DevAgent from `issue-213-plan-codex/PLAN.md` + `issue-213-plan-claude/PLAN.md` + both `CRITIQUE.md`s.**

## Problem

`subagents.agents_list` (`src/mindroom/custom_tools/subagents.py:502-513`) returns `sorted(context.config.agents.keys())` — every agent in the system, with no filter or capability flags. The `delegate.delegate_task` tool (`src/mindroom/custom_tools/delegate.py:86-88`) then strictly enforces the per-agent `agent_config.delegate_to` allowlist and rejects unknown targets with `"Cannot delegate to 'X'. Available agents: …"`.

LLM-driven agents trust the dynamic-looking `agents_list` discovery payload over the static `delegate` toolkit instructions, so they pick targets they aren't permitted to call and only learn at execution time. Wasted turn + bad UX.

## Scope (locked)

- ✅ `agents_list` returns capability rows per agent (`name`, `can_delegate`, `can_spawn`, `description`), excludes the caller, sorted by name.
- ✅ Updated docstring + `tools_metadata.json` description so the tool advertises capability semantics.
- ✅ One-line hint in `delegate_task` rejection pointing at `agents_list`.
- ❌ No `spawn_to` allowlist for `sessions_spawn` (Bas explicitly deferred).
- ❌ No teams in payload, no router rows, no `delegate.py` changes beyond the rejection hint.
- ❌ No frontend/Cinny/Element work.

## §1 — `agents_list` payload

New row schema (one per agent in `config.agents`, excluding the caller, sorted by `name` ascending):

```json
{
  "name": "openclaw",
  "can_delegate": true,
  "can_spawn": true,
  "description": "openclaw — generalist..."
}
```

Envelope unchanged: `_payload("agents_list", "ok", agents=<rows>, current_agent=<caller>)`.

**Breaking change:** `agents` field changes from `list[str]` to `list[dict]`. Acceptable per MEMORY.md "No Backward Compatibility" rule (Bas is the only user; clean code wins over compat shims).

## §2 — Per-caller allowlist source

Read at runtime, NOT at construction time, so:
- `agents_list` works for every caller without per-caller toolkit instances
- Hot-reloaded config changes take effect immediately

```python
async def agents_list(self) -> str:
    context = _get_context()
    if context is None:
        return _context_error("agents_list")

    caller_name = context.agent_name
    caller_cfg = context.config.agents.get(caller_name)              # <-- defensive .get()
    delegate_to = set(caller_cfg.delegate_to) if caller_cfg else set()

    rows = []
    for name in sorted(context.config.agents):
        if name == caller_name:
            continue
        rows.append({
            "name": name,
            "can_delegate": name in delegate_to,
            "can_spawn": True,    # always true today; reserved for future spawn allowlist
            "description": describe_agent(name, context.config),
        })

    return _payload("agents_list", "ok", agents=rows, current_agent=caller_name)
```

### §2a — Why `.get()` and NOT `Config.get_agent()`

Codex's CRITIQUE pushed for `context.config.get_agent(caller_name)` (raises `ValueError` on miss → "fail-fast"). **Rejected** because:

1. `ROUTER_AGENT_NAME = "router"` (`src/mindroom/constants.py:20`) is a real, legitimate caller identity that is **not** in `config.agents`. `agent_descriptions.describe_agent` already special-cases the router at `agent_descriptions.py:19-24` for exactly this reason.
2. The same toolkit's existing `_get_context() is None` branch (`subagents.py:504-506`) returns a graceful structured error — there is no principled reason to crash on the symmetric "caller-not-in-config" case.
3. Discovery tools should be permissive on identity edges; surfacing `can_delegate=False` everywhere when caller isn't a registered agent is a sensible answer, not an error.
4. CLAUDE.md's "Do not wrap things in try-excepts unless necessary" — `dict.get()` is the cleaner expression of "missing → empty allowlist" than `try: get_agent() except ValueError:`.

Add a one-line comment in source pointing at `agent_descriptions.py:19` as the precedent.

## §3 — Docstring + `tools_metadata.json`

New docstring (≤2 sentences, per MEMORY.md style):

```python
"""List agents this caller can interact with via delegate or sessions_spawn, with per-tool capability flags.

Each row reports `can_delegate` (per `agent_config.delegate_to` of the calling agent) and `can_spawn` (currently always true; reserved for a future per-caller spawn allowlist).
"""
```

The `can_spawn` clause is **load-bearing** — it explicitly tells the LLM that `can_spawn=False` is not a possible state today, preventing exactly the kind of second-guessing this PR is fixing. Codex's CRITIQUE proposed dropping it; **keep it**.

`tools_metadata.json` description (regenerated from `src/mindroom/tools/subagents.py` via the existing export script):

> "Discover, spawn, and communicate with sub-agent sessions. `agents_list` reports per-tool capability flags (delegate-aware)."

## §4 — `delegate_task` rejection hint (one line)

In `src/mindroom/custom_tools/delegate.py:86-88`:

```python
if agent_name not in self._delegate_to:
    available = ", ".join(self._delegate_to)
    return (
        f"Cannot delegate to '{agent_name}'. Available agents: {available}. "
        f"Run agents_list to inspect can_delegate flags."
    )
```

That's the entire `delegate.py` change. No other touches.

## §5 — Test plan

All in `tests/test_subagents.py` (no new file — Codex's plan to create `test_agents_list_delegate_alignment.py` is over-engineering for one assertion, per CLAUDE.md house style).

Five tests covering:

1. **`test_agents_list_payload_structure`** — combined assertion: envelope unchanged (`status=ok`, `current_agent=<caller>`), `agents` is a sorted list of dicts, each dict has exact keys `{name, can_delegate, can_spawn, description}`, caller is excluded, `can_spawn` is `True` for every row.
2. **`test_agents_list_can_delegate_reflects_delegate_to`** — config with caller `A` having `delegate_to=["B"]` and agents `[A, B, C, D]` → row for `B` has `can_delegate=True`, rows for `C` and `D` have `can_delegate=False`.
3. **`test_agents_list_empty_delegate_to`** — caller with `delegate_to=[]` → all rows have `can_delegate=False`.
4. **`test_agents_list_caller_not_in_config_returns_no_delegate`** — caller name not in `config.agents` (e.g. router) → no crash, all rows have `can_delegate=False` (locks in §2a behavior, prevents future regression to `get_agent()`).
5. **`test_agents_list_description_matches_describe_agent`** — `description` field == `describe_agent(name, config)` exactly (catches drift if `describe_agent` changes signature or output).

Plus one **integration test** in `tests/test_subagents.py`:

6. **`test_agents_list_can_delegate_aligns_with_delegate_tools`** — for caller `A` with `delegate_to=["B"]`:
   - assert every `can_delegate=True` row's `name` is in `DelegateTools(agent_name="A", delegate_to=["B"], …)._delegate_to`
   - assert every `can_delegate=False` row's `name` is NOT in `_delegate_to`
   - separately, exercise the rejection branch: `await tools.delegate_task("C", "task")` returns `"Cannot delegate to 'C'…"` (no AI mock needed — the rejection at `delegate.py:86` returns before `ai_response` is called)

Plus one assertion update in **`tests/test_delegate_tools.py`**: extend the existing rejection-message assertion to include the new `"Run agents_list to inspect can_delegate flags."` suffix.

Plus one **metadata assertion** in the existing `test_subagents_tool_registered_and_instantiates`: `assert TOOL_METADATA["subagents"].description == "<new pinned string>"`. The existing `tests/test_tools_metadata.py::test_export_tools_metadata_json` continues to guard JSON regeneration drift — both intentionally redundant (one pins source string, one pins JSON file).

**No full pytest suite pre-merge.** Targeted run:

```bash
nix-shell --run 'uv run pytest tests/test_subagents.py tests/test_delegate_tools.py tests/test_tools_metadata.py -x --no-cov -v'
```

Plus `pre-commit run --all-files`.

## §6 — File / LOC budget

| File | Source LOC | Test LOC |
|---|---|---|
| `src/mindroom/custom_tools/subagents.py` | +27 / −6 | — |
| `src/mindroom/custom_tools/delegate.py` | +1 / −1 | — |
| `src/mindroom/tools/subagents.py` | +1 / −1 (description string) | — |
| `src/mindroom/tools_metadata.json` | +1 / −1 (regenerated, do NOT hand-edit) | — |
| `tests/test_subagents.py` | — | +75 / −10 |
| `tests/test_delegate_tools.py` | — | +2 / −0 |
| **Totals** | **+30 / −9 source** | **+77 / −10 tests** |

## §7 — Implementer instructions

1. Branch is already at `origin/main`. FIRST commit on `issue-213` is this `FINAL-PLAN.md` (DevAgent commits before implementer starts).
2. Implementer: read `FINAL-PLAN.md`, implement exactly §1–§5, commit one focused commit (subject line per `safe-squash-merge.sh` conventions).
3. Regenerate `tools_metadata.json` via the existing export script — do NOT hand-edit JSON.
4. Run the targeted test command in §5; do not run the full suite.
5. `pre-commit run --all-files` must pass.
6. Push to `gitea`. Do NOT push to `origin`. Do NOT squash-merge to local main.
7. Report completion: commit SHA + targeted test output.

## §8 — Out of scope (do not touch)

- `sessions_spawn` allowlist semantics (deferred per Bas)
- Self-spawn behavior in `sessions_spawn` (existing default-to-caller is unrelated)
- Teams in payload
- Router rows in payload (router is a caller identity, not an agent the caller can act on)
- Anything in `delegate.py` other than the §4 one-line hint
- `tools_metadata.json` hand edits