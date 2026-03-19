# Full Documentation Audit Report

Audit date: 2026-03-18
Auditor: crew/coverage
Scope: ALL docs/ files checked against src/mindroom/ source code

## Methodology

Every docs/ file was read and cross-referenced against the corresponding source code.
For each doc file: every function name, class name, module path, config option, default value, env variable, CLI command, and API endpoint was verified.
The codebase was also scanned for features that exist but have no documentation.

## Summary

| Severity | Count |
|----------|-------|
| HIGH     | 1     |
| MEDIUM   | 1     |
| LOW      | 1     |

The documentation is **highly accurate** overall.
All major user-facing docs (configuration, features, tools, CLI, API, architecture) closely match the source code.

---

## Findings

### HIGH Severity

#### H1: TESTING.md — Stale frontend test file count
- **File**: `docs/dev/TESTING.md`
- **Line**: 26
- **Issue**: Claims "180+ frontend test files" but actual count is 28 (`find frontend -name "*.test.ts" -o -name "*.test.tsx" | wc -l` returns 28).
  A previous audit changed this from "27+" to "180+", but the previous count was based on a stale `find` result.
  The actual test file count is 28.
- **Fix**: Update to "28 frontend test files". **APPLIED.**

### MEDIUM Severity

#### M1: configuration/index.md — Missing OpenAI-compatible API env vars from central table
- **File**: `docs/configuration/index.md`
- **Issue**: The central env vars section documents all operational env vars but omits `OPENAI_COMPAT_API_KEYS` and `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED`, which are documented only in `docs/openai-api.md`.
  Users looking at the central env var reference may not discover these.
- **Evidence**: `src/mindroom/api/openai_compat.py:355-357` reads both vars from runtime env.
- **Fix**: Add an OpenAI-compatible API subsection to the Operational env vars table with cross-reference. **APPLIED.**

### LOW Severity

#### L1: dashboard.md — Culture mode list incomplete
- **File**: `docs/dashboard.md`
- **Line**: 131
- **Issue**: Culture "Mode selection" says "`automatic` (always active) or `manual` (opt-in)" but the `CultureMode` type in `src/mindroom/config/agent.py:15` is `Literal["automatic", "agentic", "manual"]`.
  The `agentic` mode is missing from the dashboard description.
- **Fix**: Add `agentic` to the mode list. **APPLIED.**

---

## Verified Correct (comprehensive)

The following docs were verified line-by-line against source code with no issues found:

### Configuration docs (docs/configuration/)
- **index.md**: All 17 top-level Config fields documented. All env var names, defaults, and descriptions match `src/mindroom/constants.py` and config models. Basic structure YAML block covers all sections accurately.
- **agents.md**: All 26 AgentConfig fields documented with correct types, defaults, and descriptions matching `src/mindroom/config/agent.py`. Private instance config, thread mode resolution, worker routing, and delegation documented accurately.
- **models.md**: ModelConfig fields (provider, id, host, api_key, extra_kwargs, context_window) match `src/mindroom/config/models.py`. Provider list matches `PROVIDER_ENV_KEYS` in constants.py. Context window 80% target correctly documented.
- **teams.md**: TeamConfig fields match `src/mindroom/config/agent.py:304-322`. Dynamic team formation and mode selection accurately described.
- **cultures.md**: CultureConfig fields match `src/mindroom/config/agent.py:325-343`. All three modes (automatic, agentic, manual) correctly documented in the config fields table.
- **router.md**: RouterConfig has only `model` field, correctly documented. Routing behavior, command handling, welcome messages, room management, and multi-human thread protection accurately described.

### Feature docs
- **memory.md**: Both backends (mem0, file) accurately documented. Memory scopes, file layout, auto-flush config all match `src/mindroom/config/memory.py` and `src/mindroom/memory/`. All auto-flush sub-fields and defaults verified correct.
- **knowledge.md**: KnowledgeBaseConfig and KnowledgeGitConfig fields match `src/mindroom/config/knowledge.py`. Private agent knowledge config matches `AgentPrivateKnowledgeConfig` in `src/mindroom/config/agent.py`.
- **voice.md**: VoiceConfig fields match `src/mindroom/config/voice.py`. STT config, intelligence config, dispatch behavior, and fallback accurately documented.
- **streaming.md**: Streaming behavior, presence-based streaming, throttling, tool call markers, cancellation, and large message handling accurately documented.
- **authorization.md**: AuthorizationConfig fields match `src/mindroom/config/auth.py`. Authorization flow order, alias resolution, agent_reply_permissions, and bot_accounts behavior correctly described.
- **scheduling.md**: Commands, timezone, persistence, and limitations accurately documented.
- **interactive.md**: JSON format, option limits (5 max), response methods, and limitations correctly documented.
- **attachments.md**: Attachment kinds, context scoping, retention (30 days), and operations accurately documented.
- **chat-commands.md**: All commands (!help, !hi, !schedule, !list_schedules, !cancel_schedule, !edit_schedule, !config, !skill) and aliases verified against `src/mindroom/commands/`.
- **images.md**: Image format detection, caption handling (MSC2530), media fallback, and persistence accurately documented.
- **matrix-space.md**: MatrixSpaceConfig fields match `src/mindroom/config/matrix.py:62-82`.
- **openclaw.md**: Preset expansion list matches `_OPENCLAW_COMPAT_PRESET_TOOLS` in `src/mindroom/config/main.py`. IMPLIED_TOOLS mapping correctly documented.
- **skills.md**: Skill format, frontmatter fields, eligibility gating, locations, and hot reloading accurately documented.
- **plugins.md**: Plugin manifest format, Python package resolution, MCP workaround, tools module example, ConfigField, and ToolManagedInitArg documented accurately.

### Architecture docs
- **architecture/index.md**: Component overview and data flow accurately described.
- **architecture/orchestration.md**: Boot sequence, hot reload, message handling, concurrency, and graceful shutdown accurately match `src/mindroom/orchestrator.py` and `src/mindroom/orchestration/`.
- **architecture/matrix.md**: Matrix client, agent users, room management, threading (MSC3440), reply chain resolution, streaming, presence, typing indicators, mentions, large messages, and identity management accurately documented.

### Tools docs
- **tools/index.md**: All 112 tool modules in `src/mindroom/tools/` are covered across the category listings. Tool presets, implied tools, and auto-dependency installation accurately documented.
- **tools/builtin.md**: All tools listed with correct names, descriptions, and config requirements. Claude agent sessions, OpenClaw compat preset expansion, and env var guidance accurate.
- **tools/mcp.md**: Correctly notes MCP is not natively supported yet and points to plugin workaround.

### Other docs
- **cli.md**: All CLI commands (version, run, doctor, connect, local-stack-setup, config, avatars) match `src/mindroom/cli/main.py`. Auto-generated help output is current.
- **dashboard.md**: All API endpoints verified against `src/mindroom/api/`. Dashboard tabs and features accurately described.
- **openai-api.md**: OpenAI-compatible API endpoints, auth, session continuity, model selection, streaming, teams, and limitations accurately documented.
- **getting-started.md**: All setup paths (hosted, Docker, manual) with correct commands and config examples. Profile aliases verified against `src/mindroom/cli/config.py`.

### Dev docs
- **dev/agent_configuration.md**: All previous audit findings (H1-H3, M1-M7, L1-L2) have been fixed. Config structure lists all 17 sections. Tool name "newspaper4k" corrected. Defaults fields complete.
- **dev/TESTING.md**: Backend test count (150+) verified correct. Frontend test count corrected in this audit.

### Undocumented features scan
No significant undocumented user-facing features were found.
All Config fields, CLI commands, API endpoints, env vars, and tool modules have corresponding documentation.
Internal modules (orchestration/, workers/, runtime_state.py, etc.) are implementation details that do not need user-facing docs.
