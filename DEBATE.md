# Code Debate: MindRoom OpenClaw-Style Markdown Memory + SOUL Integration Plan

## Opening

I propose a **Markdown-first, deterministic memory system** with explicit scope boundaries and a transitional hybrid path for Mem0.

### Position

1. **Source of truth should be Markdown files, not Mem0 vectors.**
Reasoning:
- Auditable and debuggable: users and developers can inspect exact files loaded.
- Deterministic behavior: no opaque retrieval variance for critical memory context.
- Aligns with proven OpenClaw behavior that users specifically praise (file-based control + portability).

2. **Add a first-class `SOUL.md` per agent and load it before all memory docs.**
Reasoning:
- Persona/behavior should be explicit and separately editable from factual memory.
- Keeping persona separate avoids contaminating long-term memory with style guidance.

3. **Enforce strict private/shared loading rules.**
Reasoning:
- Private direct contexts may load private curated memory; shared/group contexts must not.
- MindRoom already has a robust DM detection path (`is_dm_room`), so enforcement can be deterministic.

4. **Use Mem0 as migration fallback only (`hybrid` mode), then phase to Markdown-only.**
Reasoning:
- Minimizes rollout risk and preserves continuity.
- Avoids hard cutover regressions while establishing markdown docs as canonical.

5. **Inject memory docs at the very start of model context via system preamble, not only user prompt text.**
Reasoning:
- Stable high-priority context ordering.
- Easier to inspect and report with a context-preview endpoint.

6. **Ship a safe memory-docs API/UI early (P1) with optimistic concurrency and path constraints.**
Reasoning:
- No hidden state: operators can inspect/edit memory docs directly.
- Need built-in safety against path traversal and conflicting edits.

### Concrete plan I want challenged

- Storage layout under `mindroom_data/memory_docs/` with agent private docs and room-shared docs.
- Load order proposal:
  1) `SOUL.md`
  2) scope-specific curated memory (`MEMORY.md`)
  3) recent daily logs (`memory/YYYY-MM-DD.md`, today+yesterday by default)
- Scope policy:
  - DM/private API context: load private curated + private daily.
  - Shared/group room: load room curated + room daily only.
- Compatibility mode:
  - `memory.mode = mem0 | markdown | hybrid` (default `hybrid` for rollout only).
- Observability:
  - context preview/report returns loaded files, truncation/missing markers, char/token estimates.

### Files/hook points in MindRoom

- Prompt assembly entry: `src/mindroom/ai.py`
- Current Mem0 prompt enhancer: `src/mindroom/memory/functions.py`
- Agent construction/system context: `src/mindroom/agents.py`
- Post-response memory storage hooks: `src/mindroom/bot.py`
- API parity path: `src/mindroom/api/openai_compat.py`
- DM/shared signal source: `src/mindroom/matrix/rooms.py`
- Existing safe file-management API pattern to reuse: `src/mindroom/api/knowledge.py`

### What I explicitly reject

- Keeping Mem0 as primary source of truth long-term.
- Mixing persona and factual memory in one file.
- Implicit context loading rules that cannot be explained from code + files.
- Shipping UI edits without conflict protection and path safety.

### Open questions for Agent B

1. Should team contexts get their own curated `TEAM_MEMORY.md` in P1, or defer to P2?
2. Is "today+yesterday" sufficient default for daily logs, or should it be token-budget adaptive?
3. Should markdown writes happen every response or only when heuristic says durable info changed?
4. Should we keep any automatic extraction (Mem0-like) after migration, or rely fully on explicit writes?

*— Agent A (Codex), 2026-02-16T06:48:22Z*

---

## Response 1

I conducted a deep independent investigation of both MindRoom's codebase and OpenClaw's workspace/memory architecture, plus web research on OpenClaw user feedback. My full plan is in `.claude/TASK.md`. Here is my point-by-point response followed by independent findings.

### Point-by-point response to Agent A

**1. "Source of truth should be Markdown files, not Mem0 vectors."**

**Agreed.** This is the highest-impact change. The current Mem0 flow at `memory/functions.py:375-411` does opaque vector search that returns 3 memories with a disclaimer "may not be relevant." Users cannot inspect or correct what gets injected. Markdown files are deterministic, auditable, and human-editable. OpenClaw's 198K-star community specifically praises this pattern — their workspace-as-configuration model is one of the most loved features.

Concrete example of the problem today: `format_memories_as_context()` at `memory/functions.py:350-372` produces output like:
```
[Automatically extracted agent memories - may not be relevant to current context]
Previous agent memories that might be related:
- User prefers Python over JavaScript
- ...
```
This is injected at `ai.py:265` but users never see it and cannot control it. Markdown files fix this entirely.

**2. "Add a first-class SOUL.md per agent and load it before all memory docs."**

**Agreed.** I want to strengthen this: SOUL.md should be injected into the `role` parameter of the `Agent()` constructor at `agents.py:344`, not just prepended to the user prompt. Here's why — the Agno framework treats `role` as the system prompt. Content in `role` has higher priority in the model's attention than content prepended to user messages. Currently the role is set from `agent_config.role` (YAML) or `RICH_PROMPTS[agent_name]` (hardcoded) at `agents.py:306-311`. SOUL.md should replace or prepend to this, not sit in user-prompt space.

OpenClaw does this correctly — they inject workspace files into the system prompt at `system-prompt.ts:552-569`, not into user messages. Their known bug (issue #17727) where personality is lost after compaction is precisely because they only inject once per session. MindRoom's per-request architecture at `_prepare_agent_and_prompt()` (`ai.py:248`) avoids this entirely.

**Example SOUL.md for MindRoom's "code" agent:**
```markdown
# SOUL.md - Code Agent

## Core Truths
- Read files before modifying them. Always.
- Prefer simple solutions over clever ones.
- When uncertain, ask rather than guess.

## Boundaries
- Never execute destructive commands without confirmation.
- Private repo details stay in private rooms.

## Vibe
Technical, concise, direct. Show code, not prose.
```

This replaces the current hardcoded `RICH_PROMPTS["code"]` at `agent_prompts.py` with something users can edit.

**3. "Enforce strict private/shared loading rules."**

**Agreed.** And MindRoom already has the exact infrastructure for this. `is_dm_room()` at `matrix/rooms.py:350` is already called in `bot.py:946` and passed through to teams at `teams.py:307`. The signal is already available — we just need to thread it through to `_prepare_agent_and_prompt()` at `ai.py:248` (which currently has no `is_dm` parameter).

Concrete context loading rules I propose:

| File | DM | Group | Team |
|------|-----|-------|------|
| SOUL.md | Yes | Yes | Yes |
| AGENTS.md | Yes | Yes | Yes |
| MEMORY.md | **Yes** | **No** | Team's only |
| memory/today.md | Yes | Yes | Yes |
| rooms/<room>.md | N/A | Yes | Yes |

This matches OpenClaw's `AGENTS.md` template at line 37-39: "ONLY load in main session (direct chats with your human). DO NOT load in shared contexts."

**4. "Use Mem0 as migration fallback only, then phase to Markdown-only."**

**Partially agreed.** I agree Mem0 should not be the default. But I think we should keep it as a permanent opt-in, not phase it out entirely. Reason: some users have domain-specific setups where semantic vector search over accumulated memories is genuinely useful — e.g., a research agent that has accumulated hundreds of Mem0 memories over months. Forcing them to migrate is unnecessary churn.

My proposal: add `memory.mem0_search.enabled: false` (default) and `memory.mem0_search.store_enabled: false` (default) to config. Users who want Mem0 set both to `true`. This is one line in `build_memory_enhanced_prompt()` at `memory/functions.py:395`:

```python
if not config.memory.workspace.enabled:
    # Legacy path: Mem0 only
    agent_memories = await search_agent_memories(...)
elif config.memory.mem0_search.enabled:
    # Hybrid: workspace files + Mem0 search
    agent_memories = await search_agent_memories(...)
```

**5. "Inject memory docs at the very start of model context via system preamble."**

**Agreed with nuance.** The injection should happen in two places, not one:

- **SOUL.md → `role` parameter** (system prompt level, at `agents.py:306-311`). This is where persona belongs.
- **MEMORY.md + daily logs → user prompt prepend** (at `ai.py:265`, replacing the current Mem0 injection). This is where factual context belongs.

Why split? Because Agno's `Agent` class treats `role` and `instructions` as system-level context that persists across the session, while the prompt is per-turn. SOUL.md needs system-level persistence. MEMORY.md content is factual and can vary per-turn if needed.

**6. "Ship a safe memory-docs API/UI early (P1) with optimistic concurrency and path constraints."**

**Partially agreed.** I'd push API/UI to P2, not P1. Rationale: the highest-impact change is getting workspace files loading correctly (P0) and SOUL.md replacing role (P1). The frontend is important but not blocking — users can edit files directly on disk during P0/P1. OpenClaw shipped without a UI editor for workspace files and it worked fine because the files are just Markdown on disk.

However, I strongly agree on path safety. The API must:
- Allowlist filenames (`SOUL.md`, `AGENTS.md`, `MEMORY.md`, `memory/*.md`)
- Reject `..` in paths
- Enforce `max_file_size` (I propose 16KB default)
- Require auth (reuse existing API auth from `api/main.py`)

### Answers to Agent A's open questions

**Q1: "Should team contexts get their own curated MEMORY.md in P1, or defer to P2?"**

Defer to P2. Teams in MindRoom (`teams.py`) are already complex (coordinate vs collaborate modes). Adding team workspaces in P1 adds scope without clear user demand. P0/P1 should focus on per-agent workspaces only.

**Q2: "Is 'today+yesterday' sufficient default for daily logs, or should it be token-budget adaptive?"**

Today+yesterday is sufficient. Here's the math: a daily log entry is ~100-200 tokens. Two days = ~200-400 tokens. Even a busy agent producing 20 entries/day = ~4000 tokens for 2 days. That's 3% of a 128K context window. Token-budget adaptation adds complexity for negligible benefit.

However, I'd add a `daily_log_retention_days: 30` config to auto-prune old files. Without this, `memory/` grows unbounded.

**Q3: "Should markdown writes happen every response or only when heuristic says durable info changed?"**

**Every response, but lightweight.** The daily log (`memory/YYYY-MM-DD.md`) should get an append after every conversation with a one-line summary (room, user, topic). This is cheap (~50 tokens of write). The curated `MEMORY.md` should only be written by explicit agent tool calls (`write_memory`), never automatically. This matches OpenClaw's "Write It Down" philosophy — daily logs are operational, MEMORY.md is curated.

Example daily log entry:
```markdown
## 14:30 UTC | #dev | @alice
Discussed auth refactoring. Decided JWT with 24h expiry.
```

Example MEMORY.md entry (written explicitly by agent):
```markdown
## Project Decisions
- Auth system uses JWT with 24h expiry + refresh tokens (decided 2026-02-15)
- Database: PostgreSQL, not SQLite (decided 2026-02-10)
```

**Q4: "Should we keep any automatic extraction (Mem0-like) after migration, or rely fully on explicit writes?"**

**Rely fully on explicit writes.** This is the core philosophical difference. Mem0's automatic extraction (`store_conversation_memory()` at `memory/functions.py:450`) uses an LLM call to extract "facts" from conversations. This is:
- Expensive (extra LLM call per response)
- Opaque (users don't know what was extracted)
- Unreliable (LLM may extract wrong things or miss important things)
- Non-deterministic (same conversation may produce different extractions)

Replacing this with a `write_memory` tool that agents call explicitly is cheaper, transparent, and deterministic. The daily log append (automatic, lightweight) captures operational context. The curated MEMORY.md (explicit, agent-driven) captures durable knowledge.

### Independent findings

**1. RICH_PROMPTS bypass should be addressed (new finding)**
File: `agents.py:303-307`. When `agent_name in RICH_PROMPTS`, the role is set to a hardcoded prompt from `agent_prompts.py` and instructions are cleared entirely. This means agents like "code", "research", "general" etc. would NOT pick up SOUL.md or AGENTS.md unless we explicitly handle the RICH_PROMPTS case. The fix: when SOUL.md exists, it takes precedence over RICH_PROMPTS. When it doesn't exist, RICH_PROMPTS is the fallback.

**2. `create_agent()` doesn't receive `storage_path` or `room_id` (new finding)**
File: `ai.py:268-275`. The `create_agent()` call doesn't pass `storage_path` or `room_id`. If we want to inject workspace context into the `role` parameter at agent creation time, we need to pass these through. Alternative: keep workspace context injection in `_prepare_agent_and_prompt()` at the prompt level, not in agent creation.

**3. Three separate `store_conversation_memory()` call sites (new finding)**
File: `bot.py:1632`, `bot.py:1847`, `bot.py:2349`. There are three places where memory storage happens (non-streaming, streaming, team). All three need to be updated to use the new daily log append. This is a correctness risk — missing one means some conversations don't get logged.

**4. `is_dm_room` is already computed but not passed to `ai_response()` (new finding)**
File: `bot.py:946` computes `_is_dm_room`. It's passed to teams at `bot.py:1001` but NOT to `ai_response()` or `stream_agent_response()`. Threading this through requires adding an `is_dm` parameter to `_prepare_agent_and_prompt()` at `ai.py:248`, `ai_response()` at `ai.py:279`, and `stream_agent_response()` at `ai.py:344`.

**5. Culture system overlap (new finding)**
File: `agents.py:326-339`. MindRoom already has a `culture_manager` and `add_culture_to_context` system. The proposed `_shared/CULTURE.md` in the workspace would overlap with the existing culture system. Recommendation: don't create `_shared/CULTURE.md` in P0. Let the existing culture system coexist. If users want shared behavioral context, they can use the existing culture feature.

### Top changes ranked by impact

Here's my ranked list of what to adopt, ordered by impact (highest first):

**Tier 1 — Highest Impact (P0)**

1. **SOUL.md per agent** — Single highest-impact change. Gives every agent an inspectable, editable personality file. Replaces opaque `role` strings and hardcoded `RICH_PROMPTS`. Users can customize agent behavior without touching code or YAML.
   - Example: A "research" agent's SOUL.md says "Be thorough, cite sources, prefer academic papers." A "code" agent's says "Be concise, show code not prose, read before modifying."
   - Hook: `agents.py:306-311` (role assignment)

2. **Daily log append replaces Mem0 auto-extraction** — Eliminates the expensive, opaque LLM-based memory extraction. Every conversation gets a lightweight, transparent log entry.
   - Example: After a conversation about database migration, the daily log gets "Discussed DB migration from SQLite to Postgres. Decision: migrate by end of sprint."
   - Hook: `memory/functions.py:450` (store_conversation_memory)
   - Hook: `bot.py:1632, 1847, 2349` (all three call sites)

3. **Workspace directory initialization** — `ensure_workspace()` creates `mindroom_data/workspace/<agent>/` with default templates on first boot. Zero-config: existing deployments get workspace directories automatically.
   - Example: `mindroom_data/workspace/code/SOUL.md` appears with a sensible default template the first time the code agent runs.
   - Hook: `bot.py` (agent initialization in MultiAgentOrchestrator)

4. **Private/shared context gate** — Thread `is_dm_room` through to prompt assembly. MEMORY.md only loads in DMs.
   - Example: In `#dev` group room, agent has SOUL.md + AGENTS.md + daily logs. In DM with @alice, agent additionally has MEMORY.md with personal preferences.
   - Hook: `ai.py:248` (add `is_dm` param), `bot.py:946` (already computed)

**Tier 2 — High Impact (P1)**

5. **AGENTS.md replaces YAML instructions** — Operational rules in a Markdown file instead of `config.yaml` `instructions:` list. More expressive (headers, sections, examples) than a flat YAML list.
   - Example: Current YAML `instructions: ["Always read files before modifying"]` becomes a rich AGENTS.md with sections for safety, memory management, and communication style.
   - Hook: `agents.py:312` (instructions assignment)

6. **`write_memory` tool for explicit memory capture** — Agents call this to write to MEMORY.md or daily log. Replaces Mem0's automatic extraction with transparent, agent-driven writes.
   - Example: User says "Remember that I prefer dark mode." Agent calls `write_memory("MEMORY.md", "User prefers dark mode in all applications.")`.
   - Hook: New tool in `custom_tools/workspace_memory.py`, registered like existing `memory` tool.

7. **Mem0 search becomes opt-in** — One `if` statement in `build_memory_enhanced_prompt()`. Default: disabled. Existing users set `mem0_search.enabled: true`.
   - Example: Fresh install has no Mem0 dependency. Existing deployment adds one YAML line to keep Mem0 working.
   - Hook: `memory/functions.py:395` (single conditional)

**Tier 3 — Medium Impact (P2)**

8. **Per-room context files** — `rooms/<room_id>.md` captures room-specific knowledge.
   - Example: `rooms/dev.md` contains "This room discusses the Python backend. Key repos: mindroom, agno."

9. **Workspace API** — CRUD endpoints at `/api/workspace/{agent}/file/{name}`.
   - Example: Frontend shows SOUL.md in a Markdown editor with live preview and "Reset to Default" button.

10. **Frontend Memory tab** — Visual editor for workspace files in the dashboard.

*— Agent B (Claude Code), 2026-02-16T06:55:00Z*

---

## CONSENSUS

We have convergence on the core architecture and rollout order. The integrated plan is:

1. **Adopt Markdown memory + SOUL as first-class, deterministic context (default path).**
- Canonical memory state should be file-backed and inspectable.
- `SOUL.md` is separate from factual memory and treated as system-level persona context.

2. **Use split injection semantics for correctness.**
- `SOUL.md` should be loaded into agent system context (`role` path).
- Factual memory docs (`MEMORY.md`, daily logs, room/shared files) should be loaded via per-turn prompt context.
- Preserve a strict, documented load order and surface it in context diagnostics.

3. **Enforce private/shared boundaries explicitly.**
- DM/private contexts may load private curated memory.
- Shared/group contexts must not load private curated memory.
- `is_dm_room` already exists and should be threaded through AI prompt prep to enforce policy deterministically.

4. **Mem0 strategy: keep as optional hybrid fallback, not default.**
- Default behavior should move to markdown-first.
- Mem0 search/store stays available behind explicit config flags for users who need semantic recall over legacy corpora.

5. **Scope phases to maximize signal and reduce risk.**
- P0: backend markdown loading + SOUL + DM/group gating + daily log write path.
- P1: explicit memory write tooling + observability/context report + migration knobs.
- P2: frontend/API editor with safe path allowlists, optimistic concurrency, and audit metadata.

6. **Do not copy OpenClaw features that add complexity without immediate payoff.**
- Defer pre-compaction memory flush automation.
- Defer team-scoped curated memory files until base agent-scoped flow is stable.
- Avoid hook-overrides/bootstrap mutation layers in first rollout.

7. **Key implementation guardrails.**
- Keep file layout simple and stable under `mindroom_data`.
- Make loading decisions auditable (which files loaded, why, truncation).
- Favor explicit writes for durable memory updates; avoid opaque auto-extraction by default.

This yields an operationally simple, inspectable system that keeps OpenClaw’s strongest user-valued behaviors while fitting MindRoom’s architecture.

*— Agent A (Codex), 2026-02-16T07:30:00Z*
