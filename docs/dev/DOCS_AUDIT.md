# Full Documentation Audit Report

Audit date: 2026-03-18 (Round 4)
Auditor: crew/docs_dev
Scope: docs/dev/ files checked against src/mindroom/ source code

## Methodology

Every docs/dev/ file was read and cross-referenced against the corresponding source code.
For each doc file: every function name, class name, module path, config option, default value, env variable, CLI command, and API endpoint was verified.
The codebase was also scanned for features that exist but have no documentation.

## Summary

| Severity | Count |
|----------|-------|
| HIGH     | 1     |
| MEDIUM   | 3     |
| LOW      | 2     |

The documentation is **highly accurate** overall.
The main issues are a missing agent config field, slightly inaccurate test counts, and swapped frontend test command descriptions.

---

## Findings (Round 4)

### HIGH Severity

#### H1: agent_configuration.md — Missing `markdown` field from agent Configuration Fields

- **File**: `docs/dev/agent_configuration.md`
- **Section**: Configuration Fields (line ~150-178)
- **Issue**: The per-agent `markdown: bool | None` field exists in `AgentConfig` (`src/mindroom/config/agent.py:170`) but is not listed in the Configuration Fields section.
  This field allows per-agent override of the markdown formatting setting inherited from `defaults.markdown`.
  It appears in the YAML example at line 128 (via `include_default_tools`) but is never explained.
- **Fix**: Add `markdown` to the Configuration Fields list. **APPLIED.**

### MEDIUM Severity

#### M1: TESTING.md — Backend test count slightly inaccurate

- **File**: `docs/dev/TESTING.md`
- **Line**: 77
- **Issue**: Claims "150+ backend test files" but `find tests -name "test_*.py" | wc -l` returns 146.
  The count is close but technically overstated.
- **Fix**: Update to "145+ backend test files" (rounded down to stay accurate as files are added/removed). **APPLIED.**

#### M2: TESTING.md — Frontend test command descriptions swapped

- **File**: `docs/dev/TESTING.md`
- **Lines**: 33-37
- **Issue**: The doc describes `bun test` as "Run all tests once" and `bun run test` as "Run tests in watch mode", but both resolve to `vitest` (watch mode by default).
  The `package.json` scripts show `"test": "vitest"` (watch mode) and `"test:unit": "vitest run --reporter=verbose"` (run once).
- **Fix**: Correct the commands to show `bun run test:unit` for one-shot execution and `bun test` for watch mode. **APPLIED.**

#### M3: TESTING.md — Undocumented pytest markers and async mode

- **File**: `docs/dev/TESTING.md`
- **Issue**: `pyproject.toml` defines three pytest markers (`requires_matrix`, `e2e`, `slow`) and sets `asyncio_mode = "auto"`, but none are documented.
  Developers writing new tests won't know about these markers or the auto-async setup.
- **Fix**: Add a "Test Configuration" section documenting markers and asyncio mode. **APPLIED.**

### LOW Severity

#### L1: TESTING.md — Missing `test:unit` and `test:e2e` scripts from frontend commands

- **File**: `docs/dev/TESTING.md`
- **Lines**: 30-44
- **Issue**: `package.json` defines `test:unit` (vitest run) and `test:e2e` (e2e gmail oauth test) but these are not listed in the Running Frontend Tests section.
- **Fix**: Add the missing scripts. **APPLIED.**

#### L2: agent_configuration.md — Semantic default ambiguity for `learning` field

- **File**: `docs/dev/agent_configuration.md`
- **Line**: 160
- **Issue**: Documents `learning` as "(default: true)" but the Pydantic field default is `None` (meaning "inherit from `defaults.learning`, which defaults to `true`").
  Technically accurate in outcome but could confuse someone reading the Pydantic source.
- **Fix**: Clarify the inheritance behavior. **APPLIED.**

---

## Verified Correct

### docs/dev/agent_configuration.md

- All 17 top-level config sections listed and numbered correctly (lines 14-32) matching `Config` class in `src/mindroom/config/main.py:182-230`.
- All 6 ModelConfig fields (provider, id, host, api_key, extra_kwargs, context_window) documented correctly.
- All 9 supported providers (anthropic, openai, ollama, openrouter, gemini/google, vertexai_claude, groq, deepseek, cerebras) match `PROVIDER_ENV_KEYS` in `src/mindroom/constants.py:583-592`.
- MemoryConfig YAML example matches all fields in `src/mindroom/config/memory.py`: backend, team_reads_member_memory, embedder (provider + config), llm, file (max_entrypoint_lines), auto_flush.
- RouterConfig has only `model` field with default `"default"` — correctly documented.
- All 27 AgentConfig fields documented (after fix), matching `src/mindroom/config/agent.py:155-241`.
- TeamConfig fields (display_name, role, agents, rooms, model, mode) match `src/mindroom/config/agent.py:304-322`.
- CultureConfig fields and modes (automatic, agentic, manual) match `src/mindroom/config/agent.py:325-343`.
- KnowledgeBaseConfig and KnowledgeGitConfig fields match `src/mindroom/config/knowledge.py`.
- VoiceConfig fields (enabled, visible_router_echo, stt, intelligence) match `src/mindroom/config/voice.py`.
- AuthorizationConfig fields (global_users, room_permissions, default_room_access, aliases, agent_reply_permissions) match `src/mindroom/config/auth.py`.
- MatrixRoomAccessConfig fields match `src/mindroom/config/matrix.py:85-113`.
- MatrixSpaceConfig fields match `src/mindroom/config/matrix.py:62-82`.
- DefaultsConfig — all 16 fields documented with correct defaults matching `src/mindroom/config/models.py:15-71`.
- `_OPENCLAW_COMPAT_PRESET_TOOLS` expansion correctly lists all 8 tools plus `attachments` via `IMPLIED_TOOLS`.
- All example tool names verified against `src/mindroom/tools/` directory.

### docs/dev/TESTING.md

- Frontend test file count (28, excluding node_modules) verified correct.
- All 11 listed frontend test file paths exist.
- All 8 listed backend test file paths exist.
- `run-tests.sh` script exists.
- CI workflow `.github/workflows/pytest.yml` exists and uses Python 3.12 as documented.
- Example test patterns (TypeScript and Python) are representative of actual test code.

### docs/dev/exhaustive-live-test-checklist.md

- All CLI commands verified (doctor, run, config init/show/edit/validate/path, connect, local-stack-setup, avatars).
- All config options verified against Pydantic models (defaults, memory, matrix_room_access, matrix_space, voice, authorization, agents, teams, cultures, knowledge_bases).
- All chat commands verified (!help, !hi, !skill, !schedule, !listschedules, !cancel-schedule, !edit-schedule, !config).
- All API endpoints verified against `src/mindroom/api/` routes.
- All tool names verified against `src/mindroom/tools/` directory.
- OpenAI compat headers (X-Session-Id, X-LibreChat-Conversation-Id) and auth vars verified.
- SaaS platform structure verified.
- Config profiles (full, minimal, public, public-vertexai-anthropic) verified against `src/mindroom/cli/config.py`.

### docs/dev/mindroom-memory-consolidation-auto-flush-plan.md

- Status "~90% complete" accurately reflects implementation state.
- `memory/auto_flush.py` and `memory/_file_backend.py` exist with core auto-flush functionality.
- `MemoryAutoFlushConfig` class exists in `src/mindroom/config/memory.py` with all documented config fields and defaults.
- All config values verified: flush_interval_seconds=1800, idle_seconds=120, max_dirty_age_seconds=600, stale_ttl_seconds=86400, batch/extractor sub-configs correct.
- `_FLUSH_STATE_FILENAME = "memory_flush_state.json"` matches documented path.
- `NO_REPLY` token handling exists in auto_flush.py.
- Curation pass correctly documented as "not yet implemented".

### docs/dev/TOKEN_TRACKING_IMPLEMENTATION_PLAN.md

- Status "Not yet implemented" is accurate — none of the described files exist.
- Integration points (`src/mindroom/ai.py`, `src/mindroom/bot.py`) correctly identified.

### docs/dev/persistent-worker-runtime-plan.md

- All documented modules exist: `workers/backend.py`, `workers/manager.py`, `workers/models.py`, `workers/backends/kubernetes.py`, `workers/backends/local.py`, `workers/backends/static_runner.py`.
- `WorkerScope`, `ToolExecutionIdentity`, and workspace path resolution functions verified in `tool_system/worker_routing.py`.
- Phase completion status accurately described.

### docs/dev/runtime-path-architecture-refactor-prompt.md

- `RuntimePaths` class exists in `src/mindroom/constants.py`.
- `resolve_config_relative_path()` function exists.
- Runtime env precedence order implemented.

### docs/dev/pitches.md

- Marketing copy with no specific code references to audit.
- Technical claims (Matrix protocol, matrix-nio, long-term memory, Tuwunel) are directionally accurate.

### docs/dev/ops/README.md

- Operational decision table, just commands, and local/cluster paths are infrastructure references outside src/mindroom/ scope.

### docs/dev/general-agent-guides/

- Project-agnostic guides (init.md, commit.md, remove-cruft.md, testing-specialist.md, configfield-specialist.md) contain no MindRoom-specific code references to verify.
