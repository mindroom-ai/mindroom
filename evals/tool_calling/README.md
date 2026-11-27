# Tool-Calling Eval

Lean full-surface evaluation of MindRoom's real `create_agent` + `run_agent` tool-calling loop with deterministic verifiers.

## What It Tests

Every case builds a real MindRoom agent in a fresh temporary workspace with:
- **Inline toolkits:** `shell`, `filesystem`, `attachments` (`attachment` + `view_file`), `matrix_message`, workspace `skill` (`deploy-check`), and `dynamic_tools` (`tool_search`, `call_tool`).
- **Portable deferred toolkits (`defer: true`):** `chat_ui` (`enable_show_canvas: true`), `workflow`, `thread_tags`, `scheduler`, `memory`, `browser`.

Workspace-local tools (`run_shell_command`, `filesystem`, `skill`, `view_file`, `tool_search`, `call_tool`) execute for real against the temporary workspace. Side-effecting / Matrix / browser tools validate kwargs against each tool's real Python signature (`inspect.signature(...).bind`) and Draft 2020-12 JSON schema before returning stateful per-case JSON stubs.

**Security note:** `run_shell_command` and the `check_calc.py` verifier execute locally inside the temporary workspace with a stripped environment and isolated `HOME` / `MINDROOM_AGENT_WORKSPACE`, without container or VM sandboxing.

### Tracked Failure Kinds

- `nested_call_tool_name`: `call_tool` failed to rewrite due to missing/nested `name` or non-object `arguments`.
- `shell_argv_syntax`: `run_shell_command` passed a multi-item argv `list` with shell syntax (`|`, `&&`, `||`, `;`, `>`, `<`, `~`, `$(`, `<<`).
- `hallucinated_action`: passed `action=` to a function whose schema has no `action` parameter (e.g. `list_workflow_runs`, `list_thread_tags`, `view_file`).
- `missing_required_param`, `unexpected_param`, `invalid_param_value`, `unknown_tool`, `tool_error`.

## Usage

MindRoom logs are routed to `stderr` at `WARNING` by default so `stdout` is a clean Markdown table; pass `-v`/`--verbose` for full MindRoom logs on `stderr`.

```bash
# Configured models from ~/.mindroom-chat/config.yaml:
uv run python -m evals.tool_calling.run --model inkling-2:mini-vllm --model gemma4

# Local OpenAI-compatible server + OpenRouter baselines:
uv run python -m evals.tool_calling.run \
  --model local:inkling-2:mini-vllm \
  --base-url http://llama.local/v1 \
  --model openrouter:google/gemma-4-31b-it \
  --model openrouter:openai/gpt-5-mini \
  --out /tmp/tool-calling-eval.json

# Run a subset of cases with custom turn cap:
uv run python -m evals.tool_calling.run \
  --model local:inkling-2:mini-vllm \
  --cases canvas_dashboard,workflow_runs_check,shell_pipeline_logs \
  --max-turns 5 \
  --concurrency 1
```
