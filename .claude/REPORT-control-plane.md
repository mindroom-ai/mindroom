# Control Plane Safety Report

Branch: `security/control-plane-boundary-4`.
Base: `origin/main` at `c24f3cd05`, including PR #1176 where `!config` is disabled by default.

## Scope

Hardened chat config commands, config validation, API config writes, skills writes, plugin validation, knowledge paths, model/plugin/MCP safety, and MCP remote transports.
Kept #1176 behavior intact: `!config` stays disabled by default and still requires a global admin when enabled.
Did not include SaaS, memory, or unrelated MCP result/toolkit changes from other worker branches.

## Fixes

Chat config commands now reject privileged paths such as `authorization`, `models`, `plugins`, `knowledge_bases`, `mcp_servers`, `prompts`, `worker_egress_brokers`, `debug`, `llm_request_log_dir`, and `memory.embedder`.
Chat config output now redacts sensitive values before echoing YAML into Matrix.
Chat config preview/apply validation no longer imports or executes plugin modules.
API config and skills writes now fail closed when no dashboard auth is configured, unless `MINDROOM_UNSAFE_ALLOW_UNAUTHENTICATED_CONTROL_PLANE_WRITES=true` is explicitly set.
API config save/raw validation also avoids plugin module execution.
Local plugin paths are confined to the runtime config directory or storage root, while explicit Python package plugin specs remain allowed.
Knowledge base paths and stdio MCP `cwd` or path-like commands are confined to the runtime config directory or storage root.
Remote MCP transports validate URLs with server-fetch SSRF rules before opening SSE or streamable HTTP clients.

## Verification

Compiled touched modules and focused tests with `PYTHONPATH=$PWD/src` because the available venv points at another worktree.
Focused regression command passed with `12 passed`.
Touched-suite command passed with `387 passed, 4 warnings`.
Warnings were existing Starlette `TestClient` cookie deprecations.
Pre-commit passed `trim trailing whitespace`, `fix end of files`, `check docstring is first`, `check yaml`, `ruff check`, `ruff format`, `pyproject-fmt`, `prettier`, `eslint`, `check json`, and `pretty format json`.
Pre-commit could not complete because `uv`, `bun`, `.venv/bin/ty`, `.venv/bin/python`, and `.venv/bin/markdown-code-runner` are missing in this clean checkout environment.
