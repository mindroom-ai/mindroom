# Documentation Audit Report: docs/configuration/ vs src/mindroom/config/

**Date**: 2026-03-18
**Auditor**: crew/docs_config
**Branch**: docs/complete-audit-fixes

## Summary

Audited 6 documentation files against 9 source files.
Found **3 issues** (2 incomplete documentation, 1 missing cross-reference).
All agent/team/culture/router/model field names, types, and defaults are accurate.
**All issues fixed.**

---

## Detailed Findings

### ISSUE 1: memory.auto_flush section incomplete (docs/configuration/index.md)

**Severity**: Medium — Advanced tuning fields undocumented

The YAML reference block only shows 3 of 12+ auto_flush fields:
```yaml
auto_flush:
  enabled: false
  flush_interval_seconds: 1800
  idle_seconds: 120
```

Missing from the YAML example (all from `MemoryAutoFlushConfig` in `src/mindroom/config/memory.py`):
- `max_dirty_age_seconds` (default: 600)
- `stale_ttl_seconds` (default: 86400)
- `max_cross_session_reprioritize` (default: 5)
- `retry_cooldown_seconds` (default: 30)
- `max_retry_cooldown_seconds` (default: 300)
- `batch.max_sessions_per_cycle` (default: 10)
- `batch.max_sessions_per_agent_per_cycle` (default: 3)
- `extractor.no_reply_token` (default: "NO_REPLY")
- `extractor.max_messages_per_flush` (default: 20)
- `extractor.max_chars_per_flush` (default: 12000)
- `extractor.max_extraction_seconds` (default: 30)
- `extractor.include_memory_context.memory_snippets` (default: 5)
- `extractor.include_memory_context.snippet_max_chars` (default: 400)

**Fix**: Added all missing auto_flush fields to the YAML example block in index.md, plus a cross-reference comment to memory.md.

### ISSUE 2: memory.auto_flush already documented in memory.md (NOT AN ISSUE)

memory.md (lines 162-208) already has complete auto_flush documentation with all fields, examples, and behavior descriptions. No fix needed.

### ISSUE 3: File-based secrets (_FILE suffix) not cross-referenced in index.md env vars

**Severity**: Low — Only documented in models.md but the index.md env var tables didn't mention the `_FILE` variant.

**Fix**: Added a note about `_FILE` suffix support to the index.md API Keys section with a link to the models.md file-based secrets section.

---

## Verified Correct (No Issues Found)

### docs/configuration/agents.md
- All 26 AgentConfig fields verified against `src/mindroom/config/agent.py`:
  `display_name`, `role`, `model`, `tools`, `include_default_tools`, `skills`,
  `instructions`, `rooms`, `markdown`, `learning`, `learning_mode`, `memory_backend`,
  `private`, `knowledge_bases`, `context_files`, `thread_mode`, `room_thread_modes`,
  `num_history_runs`, `num_history_messages`, `compress_tool_results`,
  `enable_session_summaries`, `max_tool_calls_from_history`, `show_tool_calls`,
  `worker_tools`, `worker_scope`, `allow_self_config`, `delegate_to`
- All defaults match: field names, types, default values
- Private config fields (per, root, template_dir, context_files, knowledge.*) all correct
- Defaults section matches `DefaultsConfig` in `src/mindroom/config/models.py`
- Rich prompt agent names match `_RICH_PROMPTS` in `src/mindroom/agents.py`
- MAX_DELEGATION_DEPTH=3 matches `src/mindroom/custom_tools/delegate.py`

### docs/configuration/models.md
- All 6 ModelConfig fields verified against `src/mindroom/config/models.py`
- All 9 provider names verified against `src/mindroom/ai.py` provider_map + special handlers
- Context window 80% budget description matches code implementation
- File-based secrets documented correctly
- Vertex AI env vars correct

### docs/configuration/teams.md
- All 6 TeamConfig fields verified against `src/mindroom/config/agent.py`
- Dynamic team formation behavior correctly documented
- Mode descriptions match code

### docs/configuration/router.md
- RouterConfig.model field verified against `src/mindroom/config/models.py`
- Routing behavior descriptions match code in `src/mindroom/routing.py`
- Command list appears accurate

### docs/configuration/cultures.md
- All 3 CultureConfig fields verified against `src/mindroom/config/agent.py`
- Culture modes (automatic, agentic, manual) match `CultureMode` type
- Rules about one-culture-per-agent match validation in `Config.validate_culture_assignments()`

### docs/configuration/index.md (env vars)
- All env var names verified against source code
- Defaults for MATRIX_HOMESERVER ("http://localhost:8008"), MATRIX_SSL_VERIFY (true), etc. all correct
- MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS default "wait indefinitely" is correct (code loops while deadline is None)
- MINDROOM_ENABLE_AI_CACHE default true matches code
- MINDROOM_PORT default 8765 matches code
- MINDROOM_NAMESPACE validation (4-32 lowercase alphanumeric) matches code
- Provider env key names match `PROVIDER_ENV_KEYS` in `src/mindroom/constants.py`
