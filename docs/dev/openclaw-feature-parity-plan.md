# OpenClaw Feature Parity Plan (Living Document)

Last updated: 2026-02-17
Owner: MindRoom backend
Branch: openclaw-phase1-runtime-context

## Objective

Bring MindRoom's `openclaw` agent to practical OpenClaw behavioral parity:

- automatic personality + memory continuity from local files
- OpenClaw-compatible tool names and aliases
- session/subagent orchestration primitives with Matrix-native messaging
- clear fallbacks for gateway-only concepts

## Mandatory Execution Rules

These rules are required for this implementation run.

1. Frequent commits: each logical feature increment must be committed atomically.
2. Post-commit independent review: after **every** commit, run review using at least **three different sub-agents**.
3. Multi-round review: keep running review rounds and fixing findings until no unresolved review items remain for that commit line.
4. Living document: update this file after every major step with status, commit SHAs, review outcomes, and remaining work.

## Scope

### Included

- Phase 2 personality + memory auto-loading
- Phase 3 session/subagent registry foundation
- Phase 4 read-only compatibility tools + alias tools
- Phase 5 active orchestration tools (`sessions_send`, `sessions_spawn`, `subagents`, `message`)
- Phase 6 rollout/config wiring + gateway/nodes/canvas fallback behavior
- Tests and contract updates

### Excluded

- OpenClaw Gateway implementation itself (only explicit `not_configured` behavior)

## Implementation Plan

## Phase 2: Personality and Memory Auto-Loading

- Add `context_files` and `memory_dir` to `AgentConfig`.
- Load `SOUL.md`, `USER.md`, and selected memory files into role context at agent creation.
- Keep load deterministic and resilient to missing files.
- Add tests for context injection.

## Phase 3: Session/Subagent Registry Foundation

- Implement persistent lightweight registry for OpenClaw-compatible session metadata.
- Track canonical current session key from Matrix room/thread context.
- Support listing/history/status lookups from registry + existing runtime data.

## Phase 4: Read-Only Tools + Aliases

- Implement `agents_list`, `session_status`, `sessions_list`, `sessions_history`.
- Implement aliases:
  - `cron`
  - `web_search`
  - `web_fetch`
  - `exec`
  - `process`
- Ensure deterministic JSON response shapes.

## Phase 5: Active Orchestration and Matrix Messaging

- Implement `sessions_send` and `sessions_spawn` against Matrix runtime context.
- Implement `subagents` (`list`, `kill`, `steer`) on top of spawned run tracking.
- Implement `message` actions for send/reply/react/read with thread support.

## Phase 6: Rollout and Config Cleanup

- Add `openclaw_compat` to OpenClaw agent tool list.
- Remove obsolete instruction-only file-loading guidance (now automatic).
- Add `context_files` + `memory_dir` defaults in `config.yaml` and `cluster/k8s/instance/default-config.yaml`.
- Return `not_configured` for `gateway`, `nodes`, `canvas` with actionable message.

## Review Loop Protocol (Per Commit)

For each commit:

1. Run three independent reviewers:
   - `codex review`
   - `claude -p` with strict review prompt
   - `gemini -p` with strict review prompt
2. Aggregate findings into this document.
3. Fix findings in follow-up commit(s).
4. Re-run all three reviewers.
5. Only move to next logical feature when current review queue is empty.

## Test Gates

- Run targeted `pytest` for changed areas during feature commits.
- Run `pre-commit run --all-files` before final integration commit.
- Run full `pytest` before final handoff.

## Progress Tracker

| Phase | Status | Notes |
|---|---|---|
| Phase 2 | In progress | Starting implementation now |
| Phase 3 | Pending | Not started |
| Phase 4 | Pending | Not started |
| Phase 5 | Pending | Not started |
| Phase 6 | Pending | Not started |

## Commit and Review Log

### 2026-02-17

- Plan updated to living execution document with mandatory commit/review protocol.
- Next step: implement Phase 2 in first feature commit, then start 3-agent multi-round review cycle.
