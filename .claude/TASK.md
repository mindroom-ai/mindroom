# Markdown Memory & Soul System — Design Proposal

## 1. Executive Recommendation

**Replace Mem0's automatic memory extraction with a Markdown-file-based memory system inspired by OpenClaw, while keeping Mem0's vector search as an optional retrieval backend.**

The core idea: memory documents are plain Markdown files on disk, loaded into the system prompt at session start. An agent's persona lives in `SOUL.md`, operational rules in `AGENTS.md`, and accumulated memories in `MEMORY.md` + daily logs. These files are human-readable, git-friendly, and editable through a new frontend UI and API.

Mem0 stays available as an optional vector-search layer over those same Markdown files — not as the primary storage or extraction engine.

---

## 2. What to Copy from OpenClaw Exactly

### Copy directly (proven patterns):

1. **Workspace file convention** — Named Markdown files with clear purposes:
   - `SOUL.md` — persona, tone, boundaries, vibe
   - `AGENTS.md` — operational rules, session startup checklist
   - `MEMORY.md` — curated long-term memory (private contexts only)
   - `memory/YYYY-MM-DD.md` — daily append-only logs

2. **Session startup protocol** — "Before doing anything else, read SOUL.md, then MEMORY.md, then today's daily log." This is literally what OpenClaw's `AGENTS.md` template prescribes, and it works because it's deterministic: the agent always starts with the same context.

3. **Private vs shared context rule** — `MEMORY.md` is only loaded in DM/private sessions. In group rooms, agents only get `SOUL.md` + `AGENTS.md` + daily logs. This prevents personal context leaking to group participants.

4. **"Write it down" philosophy** — The agent is explicitly told that mental notes don't persist. If it wants to remember something, it must write to a file. This replaces Mem0's opaque automatic extraction with transparent, auditable writes.

5. **SOUL.md template structure** — The three-section format (Core Truths / Boundaries / Vibe) is concise and effective. Copy the structure, adapt the defaults for MindRoom's multi-agent context.

6. **Self-evolution of SOUL.md** — The instruction "If you change this file, tell the user" is a simple but powerful guardrail for agent personality evolution.

---

## 3. What to Adapt for MindRoom

| OpenClaw concept | MindRoom adaptation | Reason |
|---|---|---|
| Single-user workspace (`~/.openclaw/workspace`) | Per-agent workspace under `mindroom_data/workspace/<agent_name>/` | MindRoom runs multiple agents; each needs its own identity |
| Agent reads files via `read()` tool at session start | MindRoom pre-loads files into system prompt at agent creation time (in `create_agent()`) | Avoids extra tool calls and token waste; deterministic injection |
| `BOOTSTRAP.md` first-run ritual | Skip — MindRoom agents don't have a "first conversation" concept; they serve rooms continuously | Unnecessary complexity for a server-based system |
| `IDENTITY.md` (name, emoji, avatar) | Merge into `SOUL.md` as a section | MindRoom already has `display_name` in config; no need for a separate file |
| `USER.md` (info about the human) | Per-room context files: `mindroom_data/workspace/<agent>/rooms/<room_id>.md` | MindRoom agents serve multiple users in multiple rooms |
| Daily logs via `write()` tool | Agent has a `write_memory` tool that appends to `memory/YYYY-MM-DD.md` | Same concept, different tool name to match MindRoom conventions |
| Memory search via embeddings | Optional: keep Mem0 as a search backend over Markdown files, disabled by default | Most users won't need vector search over their own notes |
| `TOOLS.md` (local tool reference) | Not needed — MindRoom's tool system is config-driven, not workspace-file-driven | Different architecture |
| `HEARTBEAT.md` | Not needed — MindRoom has its own scheduling system | Different architecture |

---

## 4. What NOT to Copy (and Why)

1. **Memory search with multiple embedding providers** — OpenClaw's `memory-search.ts` is 300+ lines of deeply nested config merging across OpenAI, Gemini, Voyage, and local providers. This is over-engineered. MindRoom should just use its existing Mem0 embedder config if vector search is desired.

2. **Bootstrap file truncation (70% head / 20% tail)** — OpenClaw truncates large workspace files with a split that produces jarring mid-content breaks. MindRoom should enforce a hard size limit per file and warn users, not silently truncate.

3. **Agent self-editing SOUL.md without approval** — OpenClaw lets agents modify their own soul file. This is risky for a multi-agent server system. MindRoom should require human approval (via frontend) for SOUL.md changes.

4. **JSONL session storage** — OpenClaw stores full session transcripts as JSONL. MindRoom already uses Agno's SQLite sessions. No reason to switch.

5. **Lobster workflow shell** — Interesting but orthogonal. MindRoom has its own skill/tool system.

6. **The "don't ask permission, just do it" default** — Fine for a single-user personal assistant. Dangerous for a multi-agent system serving multiple rooms/users.

---

## 5. Proposed Architecture with Data Flow

### Storage Layout

```
mindroom_data/
├── workspace/                          # NEW: Markdown memory root
│   └── <agent_name>/                   # Per-agent workspace
│       ├── SOUL.md                     # Persona, tone, boundaries
│       ├── AGENTS.md                   # Operating rules, instructions
│       ├── MEMORY.md                   # Curated long-term memory
│       ├── memory/                     # Daily logs
│       │   ├── 2026-02-15.md
│       │   └── 2026-02-14.md
│       └── rooms/                      # Per-room context (P1)
│           └── <safe_room_id>.md
├── sessions/                           # Existing: Agno SQLite
├── chroma/                             # Existing: Mem0 ChromaDB (optional)
└── ...
```

> **Deferred:** `_shared/CULTURE.md` is NOT included — the existing culture system (`resolve_agent_culture` at `agents.py:326`) already handles shared behavioral context. Team workspaces (`<team_name>/`) are deferred to P2.

### Prompt Assembly Flow (Modified)

```
USER MESSAGE arrives
    │
    ▼
_prepare_agent_and_prompt()                    [ai.py:248]
    │
    ├─ 1. Create agent (EXISTING, modified)
    │      create_agent()                       [agents.py:204]
    │      ├─ identity_context (existing)
    │      ├─ datetime_context (existing)
    │      ├─ role = SOUL.md content → system prompt (NEW)
    │      │    Precedence: SOUL.md > RICH_PROMPTS > YAML config.role
    │      └─ instructions = AGENTS.md content (NEW)
    │           Precedence: AGENTS.md > RICH_PROMPTS > YAML config.instructions
    │
    ├─ 2. Load factual memory context into prompt (NEW)
    │      load_workspace_memory(agent_name, storage_path, room_id, is_dm)
    │      ├─ If is_dm: Read MEMORY.md → long-term memory block
    │      ├─ Read memory/today.md + memory/yesterday.md → recent context
    │      └─ If room_id: Read rooms/<room_id>.md → room context
    │      Returns: formatted context string, prepended to user prompt
    │
    ├─ 3. Optionally search Mem0 (EXISTING, now opt-in)
    │      build_memory_enhanced_prompt()       [memory/functions.py:375]
    │      Only runs if config.memory.mem0_search.enabled == True
    │
    └─ 4. Build thread history (EXISTING)
           build_prompt_with_thread_history()   [ai.py:194]

POST-RESPONSE:
    │
    └─ store_conversation_memory()             [memory/functions.py:450]
       Modified: appends to memory/YYYY-MM-DD.md instead of Mem0
       All three call sites updated: bot.py:1632, :1847, :2349
       Team path passes agent_names as list[str] → append to each agent's log
       (Mem0 storage remains available if config.memory.mem0_search.store_enabled)
```

### Context Loading Rules

| Context | DM / Private Room | Group Room | Injection point |
|---|---|---|---|
| `SOUL.md` | Yes | Yes | `role` param in `create_agent()` (system prompt) |
| `AGENTS.md` | Yes | Yes | `instructions` param in `create_agent()` (system prompt) |
| `MEMORY.md` | **Yes** | **No** (privacy) | User prompt prepend in `_prepare_agent_and_prompt()` |
| `memory/today.md` | Yes | Yes | User prompt prepend in `_prepare_agent_and_prompt()` |
| `memory/yesterday.md` | Yes | Yes | User prompt prepend in `_prepare_agent_and_prompt()` |
| `rooms/<room>.md` | N/A | Yes (P1) | User prompt prepend in `_prepare_agent_and_prompt()` |
| Mem0 search | If enabled | If enabled | User prompt prepend (existing path, now opt-in) |

> **DM detection:** Uses existing `is_dm_room()` at `matrix/rooms.py:350` — multi-signal classifier using `m.direct` account data, nio room model, and state events. Already computed at `bot.py:946`, needs to be threaded through to `_prepare_agent_and_prompt()` via new `is_dm: bool` parameter.

### Coexistence with Mem0

**Hybrid model, Markdown-first:**

```yaml
# config.yaml
memory:
  # NEW: Markdown workspace (default: enabled)
  workspace:
    enabled: true
    max_file_size: 16384          # 16KB per file, hard limit
    daily_log_retention_days: 30  # Auto-prune old daily logs

  # EXISTING: Mem0 vector search (default: disabled, opt-in)
  mem0_search:
    enabled: false                # Set true to also search Mem0
    store_enabled: false          # Set true to also store in Mem0

  # EXISTING: Embedder config (used by Mem0 if enabled)
  embedder:
    provider: openai
    config:
      model: text-embedding-3-small
```

**Migration path:** Mem0 is still there. Users who have it working keep it working by setting `mem0_search.enabled: true`. New users get Markdown-only by default.

---

## 6. Frontend/API Proposal

### API Endpoints (FastAPI router: `src/mindroom/api/workspace.py`)

```
GET    /api/workspace/{agent_name}/files           → List all workspace files
GET    /api/workspace/{agent_name}/file/{filename}  → Read file content
PUT    /api/workspace/{agent_name}/file/{filename}  → Update file content
DELETE /api/workspace/{agent_name}/file/{filename}  → Delete file (with confirmation)

GET    /api/workspace/{agent_name}/memory/daily     → List daily log files
GET    /api/workspace/{agent_name}/memory/daily/{date} → Read specific daily log

```

**Request/Response format:**

```python
# GET /api/workspace/code/file/SOUL.md
{
    "filename": "SOUL.md",
    "content": "# SOUL.md - Who You Are\n\n...",
    "size_bytes": 1234,
    "last_modified": "2026-02-15T10:30:00Z",
    "agent_name": "code"
}

# PUT /api/workspace/code/file/SOUL.md
{
    "content": "# SOUL.md - Who You Are\n\nUpdated content..."
}
# Returns 200 with the updated file metadata
# Returns 400 if content exceeds max_file_size
# Returns 422 if filename is not in the allowed set
```

**Security constraints:**
- Filenames are allowlisted: `SOUL.md`, `AGENTS.md`, `MEMORY.md`, and `memory/*.md`
- Path traversal protection: reject any `..` or absolute paths — reuse `_resolve_within_root()` pattern from `api/knowledge.py:39`
- Content size validation: reject files exceeding `max_file_size` with HTTP 400 (never truncate)
- Optimistic concurrency: `ETag` header on GET responses (md5 of content), `If-Match` required on PUT, return 409 Conflict on mismatch
- API key or session auth required (same as existing API endpoints)

### Frontend UX (Vite + React dashboard)

**New "Memory" tab in the agent detail view:**

```
┌─────────────────────────────────────────────────┐
│  Agent: CodeAgent                               │
│  ┌─────┬────────┬─────────┬───────┬──────────┐  │
│  │ Chat│ Tools  │ Memory  │ Skills│ Settings │  │
│  └─────┴────────┴─────────┴───────┴──────────┘  │
│                                                  │
│  ┌──────────────┐  ┌────────────────────────────┐│
│  │ SOUL.md    ✎ │  │ # SOUL.md - Who You Are    ││
│  │ AGENTS.md  ✎ │  │                            ││
│  │ MEMORY.md  ✎ │  │ ## Core Truths             ││
│  │ ─────────── │  │ Be helpful...              ││
│  │ Daily Logs   │  │                            ││
│  │  2026-02-15 │  │ ## Boundaries              ││
│  │  2026-02-14 │  │ ...                        ││
│  │  2026-02-13 │  │                            ││
│  └──────────────┘  │ [Save] [Reset to Default]  ││
│                    └────────────────────────────┘│
└─────────────────────────────────────────────────┘
```

**Key UX decisions:**
- Markdown editor with live preview (use existing `react-markdown` or `@uiw/react-md-editor`)
- "Reset to Default" button restores the bundled template
- Daily logs are read-only in the UI (they're append-only operational logs)
- SOUL.md and AGENTS.md changes take effect on next agent response (no restart needed)
- Show file size and warn at 80% of max_file_size

---

## 7. Phased Rollout Plan

### P0 — Core Markdown Memory + SOUL (MVP)

**Goal:** Replace Mem0's auto-extraction with file-based memory. SOUL.md becomes the agent's persona (system prompt). AGENTS.md becomes operational rules. Daily logs replace Mem0 auto-extraction. Mem0 becomes opt-in.

| # | Change | File(s) | Details |
|---|---|---|---|
| 1 | Add workspace config to Pydantic models | `src/mindroom/config.py:108` | Add `WorkspaceConfig` with `enabled`, `max_file_size`, `daily_log_retention_days` fields inside `MemoryConfig`. Add `Mem0SearchConfig` with `enabled`, `store_enabled` (both default `false`). |
| 2 | Create workspace module | `src/mindroom/workspace.py` (new) | Functions: `ensure_workspace(agent_name, storage_path)`, `load_workspace_memory(agent_name, storage_path, room_id, is_dm)`, `load_soul(agent_name, storage_path)`, `load_agents_md(agent_name, storage_path)`, `append_daily_log(agent_name: str \| list[str], storage_path, content)` |
| 3 | Bundle default templates | `src/mindroom/templates/SOUL.md`, `AGENTS.md` (new) | Default workspace file content, adapted from OpenClaw's Core Truths / Boundaries / Vibe structure |
| 4 | SOUL.md overrides `role` with RICH_PROMPTS handling | `src/mindroom/agents.py:303-312` | Precedence: if SOUL.md exists → use as `role` (overrides both RICH_PROMPTS and YAML). Else if `agent_name in RICH_PROMPTS` → use RICH_PROMPTS (existing). Else → use `agent_config.role` (existing). |
| 5 | AGENTS.md overrides `instructions` | `src/mindroom/agents.py:312` | If AGENTS.md exists → use as `instructions`. Else → existing RICH_PROMPTS/YAML fallback. |
| 6 | Thread `is_dm` to prompt assembly | `src/mindroom/ai.py:248`, `bot.py:946` | Add `is_dm: bool` parameter to `_prepare_agent_and_prompt()`, `ai_response()`, `stream_agent_response()`. Pass `_is_dm_room` (already computed at `bot.py:946`) through to these functions. |
| 7 | Inject workspace memory into prompt | `src/mindroom/ai.py:265` | In `_prepare_agent_and_prompt()`, call `load_workspace_memory()` to load MEMORY.md (DM only) + daily logs. Prepend result to user prompt. |
| 8 | Make Mem0 search opt-in | `src/mindroom/memory/functions.py:375` | In `build_memory_enhanced_prompt()`, check `config.memory.mem0_search.enabled` before searching. Default: disabled. |
| 9 | Replace Mem0 storage with daily log (all 3 call sites) | `src/mindroom/memory/functions.py:450`, `bot.py:1632, :1847, :2349` | In `store_conversation_memory()`, when workspace is enabled, append a summary to `memory/YYYY-MM-DD.md` instead of calling `memory.add()`. Team path at `:2349` passes `agent_names` as `list[str]` → append to each agent's daily log. |
| 10 | Initialize workspace on agent boot | `src/mindroom/bot.py` (in agent init) | Call `ensure_workspace()` during `MultiAgentOrchestrator` initialization to create directories and copy templates if missing. |
| 11 | Tests | `tests/test_workspace.py` (new) | Unit tests for workspace loading, SOUL.md role injection, RICH_PROMPTS override, DM gating, daily log appending, file size rejection. |
| 12 | Config migration | `src/mindroom/config.py` | Add backward-compatible defaults so existing `config.yaml` files work unchanged. |

### P1 — Memory Tooling & Observability

**Goal:** Agents can explicitly write to memory files. Per-room context files. Context observability for debugging.

| # | Change | File(s) | Details |
|---|---|---|---|
| 1 | Add `write_memory` agent tool | `src/mindroom/custom_tools/workspace_memory.py` (new) | Tool that agents call to write to MEMORY.md or daily log. Validates file size on write — rejects oversized content, never truncates. Registered like existing memory tool. |
| 2 | Per-room context loading | `src/mindroom/workspace.py` | Add `load_room_context(agent_name, room_id, storage_path)` that reads `rooms/<safe_room_id>.md`. |
| 3 | Context observability endpoint | `src/mindroom/api/main.py` | Add GET `/api/agents/{name}/context-report` that returns which workspace files were loaded, their sizes, and any warnings (missing files, size limits). |
| 4 | Daily log auto-pruning | `src/mindroom/workspace.py` | On workspace init, delete daily logs older than `daily_log_retention_days`. |

### P2 — Frontend, API & Team Workspace

**Goal:** Users can inspect and edit workspace files through the dashboard. Teams get shared workspaces.

| # | Change | File(s) | Details |
|---|---|---|---|
| 1 | Workspace API router | `src/mindroom/api/workspace.py` (new) | FastAPI router with CRUD endpoints for workspace files. Reuse `_resolve_within_root()` pattern from `api/knowledge.py:39`. |
| 2 | ETag/If-Match conflict protection | `src/mindroom/api/workspace.py` | GET returns `ETag` header (md5 of content). PUT requires `If-Match` header, returns 409 Conflict on mismatch. Oversized content returns 400 (never truncate). |
| 3 | Register router | `src/mindroom/api/main.py:17-27` | Add `from mindroom.api.workspace import router as workspace_router` and include it. |
| 4 | Frontend Memory tab | `frontend/src/components/AgentMemory.tsx` (new) | React component with file list + Markdown editor. |
| 5 | Frontend routing | `frontend/src/App.tsx` or equivalent | Add Memory tab to agent detail view. |
| 6 | API client functions | `frontend/src/lib/api.ts` or equivalent | Add `getWorkspaceFiles()`, `getWorkspaceFile()`, `updateWorkspaceFile()`. |
| 7 | Team workspace support | `src/mindroom/workspace.py` | Add `load_team_workspace(team_name, storage_path)` for team SOUL.md and MEMORY.md. Storage at `workspace/<team_name>/`. |

### P3 — Advanced Features (Future)

- **`_shared/` workspace:** Shared workspace files across agents (e.g., `_shared/CULTURE.md`). Only add if the existing culture system (`resolve_agent_culture`) proves insufficient.
- **Memory consolidation:** Periodic background task that reads daily logs and updates MEMORY.md with a summary (agent-driven or LLM-driven).
- **Vector search over Markdown:** Re-index workspace Markdown files into ChromaDB for semantic search. Replaces Mem0's role entirely.
- **Workspace git sync:** Optional git backup of workspace directories.
- **Memory diff view:** Frontend shows what the agent changed in its memory files, with approve/reject.

---

## 8. Test Plan and Acceptance Criteria

### Unit Tests (`tests/test_workspace.py`)

| Test | Validates |
|---|---|
| `test_ensure_workspace_creates_directories` | Workspace dirs created with correct structure |
| `test_ensure_workspace_copies_templates` | Default SOUL.md and AGENTS.md are created from templates |
| `test_ensure_workspace_idempotent` | Running twice doesn't overwrite existing files |
| `test_load_workspace_context_dm` | DM context includes SOUL.md + AGENTS.md + MEMORY.md + daily logs |
| `test_load_workspace_context_group` | Group context includes SOUL.md + AGENTS.md + daily logs but NOT MEMORY.md |
| `test_load_workspace_context_missing_files` | Graceful handling when workspace files don't exist |
| `test_append_daily_log` | Content appended to correct date file with timestamp |
| `test_daily_log_creates_directory` | memory/ dir created if missing |
| `test_file_size_limit_enforced` | Writes exceeding max_file_size are rejected with error (never truncated) |
| `test_daily_log_pruning` | Logs older than retention period are deleted |
| `test_workspace_context_in_prompt` | Full integration: workspace context appears in final prompt sent to AI |
| `test_mem0_disabled_by_default` | With default config, Mem0 search is not called |
| `test_mem0_enabled_opt_in` | With `mem0_search.enabled: true`, Mem0 search runs alongside workspace |
| `test_soul_overrides_rich_prompts` | When SOUL.md exists, it takes precedence over RICH_PROMPTS for built-in agents |
| `test_soul_fallback_to_rich_prompts` | When SOUL.md doesn't exist, RICH_PROMPTS behavior is preserved |
| `test_agents_md_overrides_yaml_instructions` | When AGENTS.md exists, it replaces YAML instructions |
| `test_is_dm_threaded_to_prompt_assembly` | `is_dm` parameter reaches `load_workspace_memory()` and gates MEMORY.md loading |
| `test_daily_log_team_multi_agent` | Team path with `list[str]` agent names appends to each agent's daily log |

### Integration Tests

| Test | Validates |
|---|---|
| `test_agent_response_uses_soul` | Agent response reflects SOUL.md personality when changed |
| `test_memory_written_after_conversation` | Daily log updated after a conversation round |
| `test_write_memory_tool` | Agent can explicitly call write_memory to update MEMORY.md (P1) |
| `test_room_context_isolation` | Different rooms get different room context files (P1) |

### API Tests

| Test | Validates |
|---|---|
| `test_api_list_workspace_files` | GET returns correct file list |
| `test_api_read_workspace_file` | GET returns file content |
| `test_api_update_workspace_file` | PUT updates file, returns new metadata |
| `test_api_path_traversal_blocked` | Filenames with `..` are rejected |
| `test_api_file_size_limit` | PUT with oversized content returns 400 |
| `test_api_allowlist_enforced` | PUT to non-allowed filename returns 422 |
| `test_api_etag_conflict` | PUT with stale `If-Match` returns 409 Conflict |
| `test_api_etag_required` | PUT without `If-Match` header returns 428 Precondition Required |

### Acceptance Criteria

1. An agent with no workspace files works identically to today (backward compatible).
2. When SOUL.md exists, it is used as the agent's `role` (system prompt), overriding both RICH_PROMPTS and YAML `role`.
3. When SOUL.md does not exist, existing RICH_PROMPTS/YAML behavior is preserved.
4. MEMORY.md is never included in group room contexts (gated by `is_dm_room()`).
5. Daily logs are appended after each conversation at all three call sites (standard, streaming, team), with timestamp and summary.
6. Oversized file writes are rejected with an error, never truncated.
7. The frontend shows workspace files and allows editing with ETag-based conflict protection (P2).
8. Existing Mem0 users can enable it alongside workspace with `mem0_search.enabled: true`.
9. All existing tests continue to pass.

---

## 9. Risks and Open Questions

### Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Context window bloat** — Large SOUL.md + MEMORY.md + daily logs could consume significant tokens | High | Hard file size limits (16KB default). Frontend warns at 80%. Daily log pruning after 30 days. |
| **Agent writes garbage to memory files** — LLM could produce malformed or unhelpful memory entries | Medium | The `write_memory` tool validates Markdown format. Daily logs are append-only (no overwrites). Users can review/edit via frontend. |
| **Breaking change for Mem0 users** — Disabling Mem0 by default could surprise existing users | Medium | Mem0 stays fully functional with `mem0_search.enabled: true`. Migration guide in release notes. |
| **File I/O performance** — Reading multiple files on every request adds latency | Low | Files are small (< 16KB), cached in memory after first read, invalidated on write. File reads are ~1ms. |
| **Concurrent agent writes to same daily log** — Multiple agents writing to the same file simultaneously | Low | Each agent has its own workspace. Team workspaces use file locking or append-only semantics. |
| **Post-compaction context loss** (OpenClaw's known bug) — After context window compaction, workspace files may be "forgotten" | Medium | MindRoom doesn't compact mid-session the same way OpenClaw does. Workspace context is injected per-request, not once per session. This is a non-issue with MindRoom's architecture. |

### Open Questions

1. **Should SOUL.md be per-agent or shared?** Decision: per-agent as default. Shared workspace (`_shared/`) deferred to P3. Existing culture system handles shared behavioral context.

2. ~~**How to determine if a room is "private"?**~~ **RESOLVED:** Use existing `is_dm_room()` at `matrix/rooms.py:350` — multi-signal classifier using `m.direct` account data, nio room model, and state events. Already computed at `bot.py:946`. No new heuristic needed.

3. **Should agents be able to edit their own SOUL.md?** OpenClaw allows it. Recommendation for P0: No. Agents can write to MEMORY.md and daily logs only. SOUL.md edits require human approval via frontend. Revisit in P3.

4. **What format for daily log entries?** Recommendation:
   ```markdown
   ## 2026-02-15 14:30 UTC

   **Room:** #dev
   **User:** @alice

   Discussed refactoring the auth module. Decided to use JWT instead of sessions.
   Key decision: token expiry set to 24h with refresh tokens.
   ```

5. **How to handle the existing `instructions` field in config.yaml?** If AGENTS.md exists, it takes full precedence (replaces, not merges). YAML `instructions` field remains as fallback only. Same precedence pattern as SOUL.md: AGENTS.md > RICH_PROMPTS embedded instructions > YAML instructions.

6. **Token budget for workspace context?** Recommendation: budget 4K tokens for workspace files (roughly: SOUL.md ~500 tokens, AGENTS.md ~500 tokens, MEMORY.md ~2K tokens, daily logs ~1K tokens). This is small relative to most model context windows (128K+).

---

## 10. Sources

### Local Code References

| File | Lines | What it shows |
|---|---|---|
| `src/mindroom/memory/functions.py` | 375-411 | `build_memory_enhanced_prompt()` — the primary injection point for memory context |
| `src/mindroom/memory/functions.py` | 350-372 | `format_memories_as_context()` — how Mem0 memories are formatted as prompt text |
| `src/mindroom/memory/functions.py` | 450-542 | `store_conversation_memory()` — post-response memory storage via Mem0 |
| `src/mindroom/ai.py` | 248-276 | `_prepare_agent_and_prompt()` — the orchestration point where memory + agent + prompt come together |
| `src/mindroom/ai.py` | 265 | Exact line where `build_memory_enhanced_prompt()` is called |
| `src/mindroom/agents.py` | 288-312 | Agent identity context, role, and instructions assembly |
| `src/mindroom/agents.py` | 341-358 | `Agent()` constructor call with all parameters |
| `src/mindroom/config.py` | 26-70 | `AgentConfig` model — where `role`, `instructions`, `knowledge_bases` are defined |
| `src/mindroom/config.py` | 108-116 | `MemoryConfig` model — current Mem0 configuration |
| `src/mindroom/agents.py` | 303-307 | `RICH_PROMPTS` bypass — SOUL.md must override this branch when present |
| `src/mindroom/agents.py` | 326-339 | `resolve_agent_culture()` — existing culture system (do not duplicate with `_shared/CULTURE.md`) |
| `src/mindroom/bot.py` | 946 | `_is_dm_room` computed — needs to be threaded to `ai_response()` and `stream_agent_response()` |
| `src/mindroom/bot.py` | 1632 | `store_conversation_memory()` call — standard (non-streaming) response path |
| `src/mindroom/bot.py` | 1847 | `store_conversation_memory()` call — streaming/cancellable response path |
| `src/mindroom/bot.py` | 2349 | `store_conversation_memory()` call — team response path (passes `list[str]` agent names) |
| `src/mindroom/matrix/rooms.py` | 350 | `is_dm_room()` — multi-signal DM classifier (m.direct, nio model, state events) |
| `src/mindroom/api/main.py` | 17-27 | API router registration — where workspace router would be added |
| `src/mindroom/api/knowledge.py` | 39-49 | `_resolve_within_root()` — path validation pattern to reuse for workspace API |

### OpenClaw Code References

| File | Lines | What it shows |
|---|---|---|
| `../openclaw/src/agents/workspace.ts` | 21-29 | Bootstrap filename constants (AGENTS.md, SOUL.md, etc.) |
| `../openclaw/src/agents/workspace.ts` | 237-291 | `loadWorkspaceBootstrapFiles()` — reads all workspace files from disk |
| `../openclaw/src/agents/system-prompt.ts` | 164-609 | `buildAgentSystemPrompt()` — full system prompt construction |
| `../openclaw/src/agents/system-prompt.ts` | 552-569 | Where workspace files are injected as "Project Context" |
| `../openclaw/src/agents/bootstrap-files.ts` | 21-41 | `resolveBootstrapFilesForRun()` — subagent file filtering |
| `../openclaw/docs/reference/templates/SOUL.md` | 1-43 | Default SOUL.md template (copied above) |
| `../openclaw/docs/reference/templates/AGENTS.md` | 1-219 | Default AGENTS.md template with session protocol |

### Internet Sources

| Source | URL | Date | Why it matters |
|---|---|---|---|
| OpenClaw memory docs | https://docs.openclaw.ai/concepts/memory.md | Current | Authoritative description of the two-layer memory architecture (daily logs + MEMORY.md) |
| OpenClaw SOUL.md template | https://docs.openclaw.ai/reference/templates/SOUL.md | Current | The proven persona template format that users praise |
| Token consumption discussion | https://github.com/openclaw/openclaw/discussions/1949 | Jan 2026 | #1 user pain point — validates the need for controlled context injection (not "load everything") |
| Post-compaction context loss | https://github.com/openclaw/openclaw/issues/17727 | Feb 2026 | Critical bug where personality/rules are lost after compaction — validates MindRoom's per-request injection approach |
| Personality enforcement failure | https://github.com/openclaw/openclaw/issues/17093 | Recent | Demonstrates that SOUL.md alone isn't enough — system-level enforcement matters |
| Tiered memory proposal | https://github.com/openclaw/openclaw/discussions/17692 | Feb 2026 | Community-validated design for memory lifecycle (daily → short-term → long-term) |
| Memory consolidation RFC | https://github.com/openclaw/openclaw/discussions/17711 | Feb 2026 | Formal proposal for memory formation, consolidation, and forgetting — relevant for P3 |
| SOUL.md community templates | https://github.com/openclaw/openclaw/discussions/17022 | Feb 2026 | Shows users actively creating and sharing persona templates — validates the concept |
| OpenClaw agent workspace docs | https://docs.openclaw.ai/concepts/agent-workspace.md | Current | Official workspace structure documentation |
| OpenClaw context management | https://docs.openclaw.ai/concepts/context.md | Current | Token budget management and compaction details |
