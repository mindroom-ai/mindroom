# Documentation Audit Report: docs/dev/ vs src/mindroom/

Audit date: 2026-03-18
Auditor: docs_dev crew agent
Scope: All files in `docs/dev/` checked against `src/mindroom/` source code

## Summary

| Severity | Count |
|----------|-------|
| HIGH     | 3     |
| MEDIUM   | 7     |
| LOW      | 2     |

---

## Findings

### HIGH Severity

#### H1: agent_configuration.md — False claim of pre-configured agents
- **File**: `docs/dev/agent_configuration.md`
- **Lines**: 84-133
- **Issue**: States "mindroom comes with several pre-configured agents" and lists 9 agents (general, calculator, code, shell, summary, research, finance, news, data_analyst) as built-in defaults.
MindRoom does NOT ship with these agents.
The `mindroom config init` command generates a single "mind" starter agent.
These 9 agents are hypothetical examples, not defaults.
- **Evidence**: `src/mindroom/cli/config.py` generates only a "mind" agent in its starter template. No other agents are built-in.
- **Fix**: Rewrite the section as example configurations, not built-in agents.

#### H2: agent_configuration.md — Wrong tool name "newspaper"
- **File**: `docs/dev/agent_configuration.md`
- **Lines**: 274, 389
- **Issue**: Lists a tool called "newspaper" for parsing news articles. The actual registered tool name is "newspaper4k".
- **Evidence**: `src/mindroom/tools/newspaper4k.py` registers with `name="newspaper4k"`. No tool named "newspaper" exists.
- **Fix**: Replace "newspaper" with "newspaper4k" in all occurrences.

#### H3: agent_configuration.md — Missing major config sections from overview
- **File**: `docs/dev/agent_configuration.md`
- **Lines**: 9-18
- **Issue**: Claims the config has 5 main sections (memory, models, agents, defaults, router).
The actual `Config` model in `src/mindroom/config/main.py` has 17 top-level fields: agents, teams, cultures, room_models, plugins, defaults, memory, knowledge_bases, models, router, voice, timezone, mindroom_user, matrix_room_access, matrix_space, authorization, bot_accounts.
12 top-level sections are undocumented in the overview.
- **Evidence**: `src/mindroom/config/main.py:182-231` defines the `Config` class with all fields.
- **Fix**: Expand the configuration structure overview to list all sections.

### MEDIUM Severity

#### M1: agent_configuration.md — Incomplete embedder provider list
- **File**: `docs/dev/agent_configuration.md`
- **Line**: 64
- **Issue**: Lists embedder providers as "openai, ollama, sentence_transformers" but the code documents more options including "huggingface".
- **Evidence**: `src/mindroom/config/memory.py:19` says "openai, ollama, huggingface, sentence_transformers, etc".
- **Fix**: Update the provider list to match the code.

#### M2: agent_configuration.md — context_files path description uses stale convention
- **File**: `docs/dev/agent_configuration.md`
- **Lines**: 183, 203
- **Issue**: Describes context_files paths as "relative to `agents/<name>/workspace/`". The actual path is resolved by `agent_workspace_relative_path()` in `src/mindroom/tool_system/worker_routing.py` and the canonical workspace root convention is `<storage_root>/agents/<name>/workspace/`.
- **Evidence**: `src/mindroom/config/agent.py:290` validates via `agent_workspace_relative_path()`.
- **Fix**: Update the path description to reference the canonical agent workspace root.

#### M3: TESTING.md — Stale frontend test file count
- **File**: `docs/dev/TESTING.md`
- **Line**: 26
- **Issue**: Claims "27+ frontend test files" but there are actually 182+ frontend test files (`.test.ts` and `.test.tsx`).
- **Evidence**: `find frontend -name "*.test.ts" -o -name "*.test.tsx" | wc -l` returns 182.
- **Fix**: Update the count.

#### M4: agent_configuration.md — Many tools undocumented
- **File**: `docs/dev/agent_configuration.md`
- **Lines**: 253-284
- **Issue**: Documents ~20 tools but the codebase has 100+ registered tools in `src/mindroom/tools/__init__.py`. Major categories missing: AI/generation (dalle, fal, replicate, gemini, openai, claude_agent), collaboration (slack, discord, whatsapp, webex, zoom), project management (jira, linear, clickup, trello, todoist, notion), search (tavily, exa, searxng, serpapi, serper, crawl4ai, firecrawl), and many more.
- **Evidence**: `src/mindroom/tools/__init__.py` registers 90+ tool modules.
- **Fix**: Add a note that this is a partial list and reference the dashboard for full tool discovery.

#### M5: agent_configuration.md — Missing documentation for teams, cultures, knowledge_bases
- **File**: `docs/dev/agent_configuration.md`
- **Issue**: The doc covers agents in detail but does not document teams (TeamConfig), cultures (CultureConfig), or knowledge_bases (KnowledgeBaseConfig) at all despite these being core config sections.
- **Evidence**: `src/mindroom/config/agent.py:304-343` (TeamConfig), `src/mindroom/config/agent.py:325-343` (CultureConfig), `src/mindroom/config/knowledge.py:36-62` (KnowledgeBaseConfig).
- **Fix**: Add sections for teams, cultures, and knowledge bases.

#### M6: agent_configuration.md — Missing voice, authorization, matrix_room_access documentation
- **File**: `docs/dev/agent_configuration.md`
- **Issue**: No documentation for voice configuration (VoiceConfig), authorization (AuthorizationConfig), or matrix_room_access (MatrixRoomAccessConfig) despite these being actively used config sections.
- **Evidence**: `src/mindroom/config/voice.py`, `src/mindroom/config/auth.py`, `src/mindroom/config/matrix.py`.
- **Fix**: Add sections for these config areas.

#### M7: agent_configuration.md — Missing defaults fields documentation
- **File**: `docs/dev/agent_configuration.md`
- **Lines**: 454-458
- **Issue**: The defaults example only shows `tools`, `markdown`, `enable_streaming`. Missing fields: `show_stop_button`, `learning`, `learning_mode`, `num_history_runs`, `num_history_messages`, `compress_tool_results`, `enable_session_summaries`, `max_tool_calls_from_history`, `show_tool_calls`, `worker_tools`, `worker_scope`, `allow_self_config`, `max_preload_chars`.
- **Evidence**: `src/mindroom/config/models.py:15-72` (DefaultsConfig).
- **Fix**: Document all defaults fields.

### LOW Severity

#### L1: TESTING.md — test file list may be stale
- **File**: `docs/dev/TESTING.md`
- **Lines**: 13-24
- **Issue**: Lists specific frontend test files as "representative examples" which is acceptable, but the qualifier "non-exhaustive" could be more prominent since the actual count (182) is 7x the claimed count (27+).
- **Fix**: Already addressed by M3 count fix.

#### L2: agent_configuration.md — Minor: "jina" description vague
- **File**: `docs/dev/agent_configuration.md`
- **Line**: 276
- **Issue**: Describes jina as "Advanced document processing" but the tool is primarily for web reading/search via the Jina Reader API, not general document processing.
- **Fix**: Update description to "Web reading and search via Jina Reader API".

---

## Verified Correct

The following claims were verified as accurate:
- All agent config field names match Pydantic models
- Default values (include_default_tools=true, learning=true, learning_mode="always", thread_mode="thread") are correct
- Memory backends ("mem0", "file") are correctly documented
- Model provider list (anthropic, openai, ollama, openrouter, gemini/google, vertexai_claude, groq, deepseek, cerebras) is correct
- Worker scope values (shared, user, user_agent) are correct
- Thread mode values (thread, room) are correct
- Learning mode values (always, agentic) are correct
- Memory auto-flush config surface matches `MemoryAutoFlushConfig` exactly
- Token tracking plan correctly marked as "Not yet implemented"
- Persistent worker runtime plan status updates match code state
- CI workflow uses Python 3.12 as documented in TESTING.md
- `run-tests.sh` exists as referenced in TESTING.md
- 150+ backend test files exist as claimed in TESTING.md

## Plan/Archive docs (no code accuracy issues)

These docs are design plans, prompts, or marketing material with no direct code accuracy claims to verify:
- `docs/dev/pitches.md` — Marketing copy, no code references
- `docs/dev/runtime-path-architecture-refactor-prompt.md` — Agent prompt template
- `docs/dev/general-agent-guides/*` — Project-agnostic coding guides
- `docs/dev/archive/*` — Historical planning docs
- `docs/dev/security/*` — Security review checklists (policy-focused)
