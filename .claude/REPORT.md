# MindRoom Security Fix Report

## Scope

Owned scope: self_config, sandbox proxy/default execution policy, and Docker tool privilege metadata.

## Validated Findings

CONFIG-CONFIG-6 and TOOLS-TOOLS-4 were real.
`self_config` previously allowed agents to request tool, model, knowledge-base, skill, default-tool-inheritance, and context-file changes.

TOOLS-TOOLS-9 was real.
Worker-target execution tools could fall back to host execution when no sandbox proxy or dedicated worker was configured.

TOOLS-TOOLS-7 was real.
Docker was advertised as an available primary-runtime tool with no special setup, which made host Docker daemon access look safe by default.

TOOLS-TOOLS-6 is covered only for execution-tool default routing.
This patch does not make a broader unrelated policy change outside self-config, sandbox routing, and Docker metadata.

## Fixes

`self_config` now uses a positive allowlist.
Agents can still change presentation and behavior fields like role, instructions, rooms, markdown, learning settings, thread mode, and history knobs.
Agents cannot self-change tools, model, knowledge bases, skills, `include_default_tools`, or context files.

Sandbox routing now fails closed for local execution tools by default.
`shell`, `python`, `coding`, `file`, and `docker` route through a worker/proxy path unless explicitly disabled by `MINDROOM_SANDBOX_EXECUTION_MODE=off|local|disabled` or `MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS=true`.

Docker metadata now marks Docker as `requires_config`, `special`, and worker-targeted by default.
`src/mindroom/tools_metadata.json` was regenerated after the metadata change.

## Validation

Ran dependency sync:

```bash
UV_PYTHON=3.13 /Users/bas.nijholt/.local/bin/uv sync --all-extras
```

Ran focused tests:

```bash
/Users/bas.nijholt/.local/bin/uv run pytest tests/test_self_config.py tests/test_sandbox_proxy.py tests/test_tools_metadata.py -n 0 --no-cov -q
```

Result: `194 passed, 1 skipped, 46 warnings`.

Ran attachment spillover tests:

```bash
/Users/bas.nijholt/.local/bin/uv run pytest tests/test_attachments_tool.py -n 0 --no-cov -q
```

Result: `39 passed`.

Ran focused lint:

```bash
/Users/bas.nijholt/.local/bin/uv run ruff check src/mindroom/custom_tools/self_config.py src/mindroom/tool_system/sandbox_proxy.py src/mindroom/runtime_env_policy.py src/mindroom/tools/docker.py tests/test_self_config.py tests/test_sandbox_proxy.py tests/test_tools_metadata.py tests/test_attachments_tool.py
```

Result: `All checks passed!`.

## Residual Risk

Full `pytest` and full pre-commit were not run.
Focused coverage includes changed self-config behavior, sandbox proxy routing, Docker metadata export, tool metadata consistency, and attachment save routing.
