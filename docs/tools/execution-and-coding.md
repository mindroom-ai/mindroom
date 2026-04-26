---
icon: lucide/wrench
---

# Execution & Coding

Use these tools to inspect and edit local files, run shell commands or Python, work in a code-aware workspace, manage Docker resources, and generate local artifacts such as exports and charts.

## What This Page Covers

This page documents the built-in tools in the `execution-and-coding` group.
Use these tools when you need local execution, coding-oriented file access, lightweight computation, or artifact generation inside the agent runtime.

## Tools On This Page

- [`file`] - Generic local file reads, writes, listings, searches, and chunk edits.
- [`shell`] - Shell command execution with background handles, runtime env passthrough, and PATH overrides.
- [`python`] - Python code execution, file helpers, and package installation in the active runtime.
- [`coding`] - Code-oriented reads, precise edits, grep, file discovery, and directory listing.
- [`docker`] - Local Docker container, image, volume, and network management.
- [`calculator`] - Exact arithmetic and small numeric helper functions.
- [`reasoning`] - Internal `think` and `analyze` scratchpad tools for structured reasoning.
- [`file_generation`] - JSON, CSV, PDF, and text file export helpers.
- [`visualization`] - Matplotlib-backed chart generation.
- [`sleep`] - Intentional delays and pauses.

## Common Setup Notes

All ten tools on this page are exposed as `setup_type: none` in the live tool registry, so they do not require dashboard OAuth setup or credential forms before they appear as available.
`src/mindroom/api/integrations.py` currently has no dedicated integration endpoints for them because they are local-runtime tools rather than OAuth-backed services.
MindRoom's built-in default worker-routed set is `coding`, `file`, `python`, and `shell`.
You can override the effective routed set with `defaults.worker_tools` or `agents.<name>.worker_tools`.
When `worker_scope` is unset, worker-routed calls still execute in the sandbox, but they use a fresh runtime per call instead of a persistent scoped worker.
`worker_scope: shared` reuses one runtime per agent, `worker_scope: user` reuses one runtime per requester across that requester's agents, and `worker_scope: user_agent` reuses one runtime per requester-agent pair.
`worker_scope` controls runtime reuse, not filesystem security.
Use [Sandbox Proxy Isolation](../deployment/sandbox-proxy.md) for the deployment model, storage visibility rules, and scope tradeoffs.
When an agent has a canonical workspace root, MindRoom injects that workspace as `base_dir` for tools that expose a `base_dir` constructor field.
In normal `config.yaml` authoring, `base_dir` is therefore usually runtime-managed instead of something you set inline.
Those workspace-backed agents also receive the optional `mindroom_output_path` argument on eligible tools.
Set it to a workspace-relative file path to save the full supported tool output to that file and return a compact receipt to the model.
Agents without a resolved workspace do not receive this argument.
No extra configuration is required beyond that workspace root, and `MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES` only overrides the default 64 MiB per-output write cap.
Missing optional dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
That matters most here for `docker`, `file_generation`, and `visualization`, which depend on Docker access, `reportlab`, and `matplotlib`.

```yaml
defaults:
  worker_scope: user_agent

agents:
  code:
    display_name: Code
    role: Edit code, run local checks, and export artifacts
    model: sonnet
    memory_backend: file
    tools:
      - coding
      - file
      - python:
          restrict_to_base_dir: true
      - shell:
          extra_env_passthrough:
            - GITHUB_TOKEN
            - INTERNAL_API_*
          shell_path_prepend:
            - /run/wrappers/bin
            - /opt/custom/bin
      - file_generation:
          output_directory: exports
      - visualization:
          output_dir: charts
```

## [`file`]

`file` is the generic local filesystem toolkit for read, write, list, search, delete, and chunk-based edits.

### What It Does

`file` exposes `save_file()`, `read_file()`, `delete_file()`, `list_files()`, `search_files()`, `read_file_chunk()`, and `replace_file_chunk()`.
The underlying Agno toolkit resolves paths against `base_dir` and rejects paths that escape that root.
`read_file()` enforces `max_file_length` and `max_file_lines`, and it tells the caller to use chunk reads when a file is too large.
`search_files()` uses glob patterns relative to `base_dir` rather than full-text search.
MindRoom marks `file` as worker-routed by default, so it usually executes in the sandboxed worker runtime unless you override `worker_tools`.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `base_dir` | `text` | `no` | `null` | Runtime-managed working root when an agent workspace exists, otherwise the current directory. This field is not normally authored inline in `config.yaml`. |
| `enable_save_file` | `boolean` | `no` | `true` | Enable `save_file()`. |
| `enable_read_file` | `boolean` | `no` | `true` | Enable `read_file()`. |
| `enable_delete_file` | `boolean` | `no` | `false` | Enable `delete_file()`. |
| `enable_list_files` | `boolean` | `no` | `true` | Enable `list_files()`. |
| `enable_search_files` | `boolean` | `no` | `true` | Enable `search_files()`. |
| `enable_read_file_chunk` | `boolean` | `no` | `true` | Enable `read_file_chunk()`. |
| `enable_replace_file_chunk` | `boolean` | `no` | `true` | Enable `replace_file_chunk()`. |
| `expose_base_directory` | `boolean` | `no` | `false` | Include absolute file paths and `base_directory` in `search_files()` output. |
| `max_file_length` | `number` | `no` | `10000000` | Maximum character count for `read_file()`. |
| `max_file_lines` | `number` | `no` | `100000` | Maximum line count for `read_file()`. |
| `line_separator` | `text` | `no` | `"\n"` | Separator used by the chunk helpers. |
| `all` | `boolean` | `no` | `false` | Enable every upstream `file` function at once. |

### Example

```yaml
agents:
  editor:
    tools:
      - file:
          enable_delete_file: true
          max_file_lines: 2000
```

```python
read_file("README.md")
read_file_chunk("src/mindroom/tools/file.py", 0, 80)
replace_file_chunk("docs/notes.md", 10, 12, "Updated text")
list_files(directory="src")
search_files("**/*.py")
save_file("temporary notes\n", "scratch/notes.txt")
```

### Notes

- `file` is the compatibility-friendly general file toolkit, but `coding` is a better default for code-editing agents.
- `delete_file()` is disabled by default, so destructive access is opt-in.
- `search_files()` matches filesystem globs, not content inside files.

## [`shell`]

`shell` runs argv-style commands and is MindRoom's most configurable execution tool for sandboxed command-line work.

### What It Does

`shell` exposes `run_shell_command()`, `check_shell_command()`, and `kill_shell_command()`.
`run_shell_command()` expects a list of arguments, not a shell-parsed string.
If the command exits within `timeout`, the tool returns the last `tail` lines of stdout, or stderr on non-zero exit.
If the timeout is exceeded, the process keeps running in the background and the tool returns a `shell:...` handle.
Use `check_shell_command(handle)` to poll a backgrounded command and `kill_shell_command(handle)` to stop it.
MindRoom keeps up to 16 backgrounded shell processes per runner and automatically sweeps finished handle records after roughly 10 minutes.
Unlike upstream Agno's simple shell wrapper, MindRoom injects the stricter sandbox runtime env for proxied execution, supports explicit exported-process-env passthrough patterns, and supports PATH prepends.
MindRoom marks `shell` as worker-routed by default, so it usually executes in the sandboxed worker runtime.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `base_dir` | `text` | `no` | `null` | Runtime-managed working directory when an agent workspace exists. This field is not normally authored inline in `config.yaml`. |
| `enable_run_shell_command` | `boolean` | `no` | `true` | Enable `run_shell_command()` and the companion handle APIs. |
| `all` | `boolean` | `no` | `false` | Enable all shell functions. |
| `extra_env_passthrough` | `text` | `no` | `null` | Extra exported process env var names or glob patterns exposed to shell execution in addition to MindRoom's sandbox runtime env. This matches exported process env, not config-adjacent `.env` entries. |
| `shell_path_prepend` | `text` | `no` | `null` | Extra PATH entries prepended for shell subprocesses only. |

### Example

```yaml
agents:
  ops:
    worker_scope: user_agent
    tools:
      - shell:
          extra_env_passthrough:
            - GITHUB_TOKEN
            - INTERNAL_API_*
          shell_path_prepend:
            - /run/wrappers/bin
            - /opt/custom/bin
```

```python
run_shell_command(["git", "status", "--short"], tail=50)
run_shell_command(["bash", "-lc", "sleep 300 && echo done"], timeout=2)
check_shell_command("shell:abcd1234")
kill_shell_command("shell:abcd1234")
```

### Notes

- `extra_env_passthrough` only affects `shell` and matches exported process env, not config-adjacent `.env` entries. MindRoom drops runner control names (`MINDROOM_API_KEY`, `MINDROOM_LOCAL_CLIENT_SECRET`, `MINDROOM_SANDBOX_PROXY_TOKEN`, `MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH`) and any name starting with `MINDROOM_SANDBOX_`; everything else that matches passes through, including service tokens and provider credentials.
- In authored YAML, `extra_env_passthrough` and `shell_path_prepend` can be written as lists, and MindRoom normalizes them to the tool's comma-or-newline form.
- Background handles survive multiple requests to the same long-lived runner process, but they do not survive runner restarts.
- `shell_path_prepend` deduplicates PATH entries and only changes subprocess PATH, not the main MindRoom process PATH.
- For per-workspace env that an agent can edit on the fly (PATH prefixes, `NPM_CONFIG_PREFIX`, `PIP_INDEX_URL`, etc.), drop a `.mindroom/worker-env.sh` script in the workspace and `export` the values you want — see "Workspace env hook" in `docs/deployment/sandbox-proxy.md`. Example:

```bash
mkdir -p .mindroom .local/bin .cache/npm
cat > .mindroom/worker-env.sh <<'EOF'
export NPM_CONFIG_PREFIX="$PWD/.local"
export NPM_CONFIG_CACHE="$PWD/.cache/npm"
export PATH="$PWD/.local/bin:$PATH"
EOF
```

## [`python`]

`python` executes arbitrary Python code, runs Python files, exposes a few file helpers, and can install packages into the active interpreter environment.

### What It Does

`python` exposes `save_to_file_and_run()`, `run_python_code()`, `pip_install_package()`, `uv_pip_install_package()`, `run_python_file_return_variable()`, `read_file()`, and `list_files()`.
The upstream toolkit can execute arbitrary Python code and warns that it should be used with human supervision.
MindRoom wraps the installer functions so both `pip_install_package()` and `uv_pip_install_package()` install into the current interpreter environment through MindRoom's shared installer path.
When `python` is worker-routed, that means package installs land in the active worker environment rather than the primary agent process.
MindRoom marks `python` as worker-routed by default, so sandbox execution is the normal path when a sandbox backend is configured.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `base_dir` | `text` | `no` | `null` | Runtime-managed working root when an agent workspace exists. This field is not normally authored inline in `config.yaml`. |
| `safe_globals` | `text` | `no` | `null` | Advanced raw constructor input that maps to the upstream `safe_globals` dict parameter. |
| `safe_locals` | `text` | `no` | `null` | Advanced raw constructor input that maps to the upstream `safe_locals` dict parameter. |
| `restrict_to_base_dir` | `boolean` | `no` | `true` | Constrain the file helper methods to `base_dir`. |

### Example

```yaml
agents:
  analyst:
    tools:
      - python:
          restrict_to_base_dir: true
```

```python
run_python_code("total = sum(i * i for i in range(10))", variable_to_return="total")
save_to_file_and_run("scripts/demo.py", "result = 6 * 7", variable_to_return="result")
run_python_file_return_variable("scripts/demo.py", variable_to_return="result")
pip_install_package("rich")
read_file("scripts/demo.py")
list_files()
```

### Notes

- `restrict_to_base_dir` only constrains the file helper paths, not what arbitrary Python code can do once executed.
- `safe_globals` and `safe_locals` are exposed directly from the upstream constructor and are mainly useful for advanced programmatic wiring, not typical hand-written YAML.
- If you need runtime-scoped environment isolation, rely on worker-routed execution instead of assuming in-process Python emulation is a security boundary.
- Worker-routed `python` execution also receives `.mindroom/worker-env.sh` overlay env via `os.environ` (e.g., `PIP_INDEX_URL`, `UV_CACHE_DIR`). Worker-routed `coding` and `file` subprocesses receive the same overlay as process env, though their documented behavior is file operations rather than env inspection. See "Workspace env hook" in `docs/deployment/sandbox-proxy.md`.

## [`coding`]

`coding` is MindRoom's code-oriented local toolkit with line-numbered reads, precise text edits, grep, file discovery, and directory listing.

### What It Does

`coding` exposes `read_file()`, `edit_file()`, `write_file()`, `grep()`, `find_files()`, and `ls()`.
`read_file()` adds line numbers and pagination hints when output is truncated.
`edit_file()` requires the old text to match exactly one location and returns a unified diff after the edit.
If exact matching fails, `edit_file()` falls back to whitespace-and-Unicode-normalized fuzzy matching.
`grep()` prefers `rg` when available and falls back to Python regex search otherwise.
`find_files()` filters hidden and gitignored paths, and `ls()` keeps dotfiles visible while adding `/` markers to directories.
All path resolution stays inside `base_dir`.
MindRoom marks `coding` as worker-routed by default.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `base_dir` | `text` | `no` | `null` | Runtime-managed working directory for code operations when an agent workspace exists. This field is not normally authored inline in `config.yaml`. |

### Example

```yaml
agents:
  code:
    tools:
      - coding
```

```python
read_file("src/mindroom/tools/shell.py", offset=1, limit=120)
grep("default_execution_target", path="src/mindroom/tools")
find_files("**/*.md", path="docs")
edit_file("docs/example.md", "old text", "new text")
write_file("scratch/todo.txt", "first line\nsecond line\n")
ls("src/mindroom")
```

### Notes

- Prefer `coding` over `file` for code-editing agents because it gives better read pagination, better search, and safer text replacement behavior.
- Recursive `grep()` and `find_files()` filter hidden and gitignored paths automatically, but explicit file targets are not filtered.
- `edit_file()` refuses ambiguous edits, so widen the surrounding context in `old_text` when a match is not unique.

## [`docker`]

`docker` manages local containers, images, volumes, and networks through the Docker Python client.

### What It Does

`docker` exposes container operations such as `list_containers()`, `run_container()`, `exec_in_container()`, `start_container()`, `stop_container()`, `remove_container()`, `get_container_logs()`, and `inspect_container()`.
It also exposes image, volume, and network operations including `pull_image()`, `build_image()`, `tag_image()`, `list_volumes()`, `create_volume()`, `list_networks()`, and `connect_container_to_network()`.
On startup, the toolkit checks common Docker socket locations and pings the Docker daemon.
`docker` defaults to primary execution, so it normally runs beside the main agent process rather than in the worker sandbox.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  platform:
    tools:
      - docker
```

```python
list_containers()
run_container("postgres:16", detach=True, environment={"POSTGRES_PASSWORD": "example"})
get_container_logs("postgres", tail=50)
list_images()
list_networks()
```

### Notes

- The runtime that executes `docker` must have access to a working Docker socket or daemon.
- If you explicitly route `docker` through workers with `worker_tools`, the worker runtime also needs Docker access.
- `get_container_logs(stream=True)` does not return a live stream payload to the model and instead returns a status message telling you to use non-streaming mode.

## [`calculator`]

`calculator` provides exact small-math helper functions without requiring arbitrary code execution.

### What It Does

`calculator` exposes `add()`, `subtract()`, `multiply()`, `divide()`, `exponentiate()`, `factorial()`, `is_prime()`, and `square_root()`.
Each function returns a small JSON payload describing the operation and result.
`calculator` defaults to primary execution.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  math:
    tools:
      - calculator
```

```python
add(2, 3)
divide(22, 7)
factorial(6)
is_prime(97)
square_root(144)
```

### Notes

- Errors such as division by zero, negative factorials, and negative square roots are returned as JSON error payloads instead of raising Python exceptions into the model.
- Use `calculator` for exact arithmetic when you do not need the broader power and risk of `python`.

## [`reasoning`]

`reasoning` gives an agent an internal scratchpad for structured `think` and `analyze` steps.

### What It Does

`reasoning` exposes `think()` and `analyze()`.
Both functions write reasoning steps into run-scoped session state keyed by the current Agno `run_id`.
`think()` records an intermediate thought plus an optional next action.
`analyze()` records the result of a prior step and maps `next_action` onto `continue`, `validate`, or `final_answer`.
These steps are intended for the agent's internal reasoning flow rather than user-visible output.
`reasoning` defaults to primary execution.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_think` | `boolean` | `no` | `true` | Enable `think()`. |
| `enable_analyze` | `boolean` | `no` | `true` | Enable `analyze()`. |
| `add_instructions` | `boolean` | `no` | `false` | Inject the toolkit's reasoning instructions into the model prompt. |
| `add_few_shot` | `boolean` | `no` | `false` | Append built-in or custom few-shot examples when instructions are being added. |
| `instructions` | `text` | `no` | `null` | Replace the default reasoning instructions with your own text. |
| `few_shot_examples` | `text` | `no` | `null` | Provide custom few-shot examples for the reasoning toolkit. |
| `all` | `boolean` | `no` | `false` | Enable all reasoning functions. |

### Example

```yaml
agents:
  researcher:
    tools:
      - reasoning:
          add_instructions: true
          add_few_shot: true
```

```python
think(
    title="Plan the investigation",
    thought="I should inspect the config and then compare it with the runtime behavior.",
    action="Read the relevant files.",
)
analyze(
    title="Evaluate the findings",
    result="The config and runtime behavior match.",
    analysis="I have enough information to answer clearly.",
    next_action="final_answer",
)
```

### Notes

- `instructions` replaces the default reasoning prompt text entirely.
- `few_shot_examples` only matters when few-shot examples are actually being included.
- The stored reasoning steps live in session state for the current run, which lets later steps see the full scratchpad history.

## [`file_generation`]

`file_generation` creates export artifacts as JSON, CSV, PDF, or plain text and can optionally save them to disk.

### What It Does

`file_generation` exposes `generate_json_file()`, `generate_csv_file()`, `generate_pdf_file()`, and `generate_text_file()`.
Each function returns a `ToolResult` with a generated file artifact attached.
If `output_directory` is set, the generated file is also written to disk and the result message includes that file path.
If `output_directory` is unset, the file still exists in the tool result payload but is not persisted to disk by the toolkit itself.
PDF generation is automatically disabled when `reportlab` is unavailable, even if `enable_pdf_generation` is left on.
`file_generation` defaults to primary execution.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `output_directory` | `text` | `no` | `null` | Optional directory where generated files are written to disk. |
| `enable_json_generation` | `boolean` | `no` | `true` | Enable `generate_json_file()`. |
| `enable_csv_generation` | `boolean` | `no` | `true` | Enable `generate_csv_file()`. |
| `enable_pdf_generation` | `boolean` | `no` | `true` | Enable `generate_pdf_file()` when `reportlab` is available. |
| `enable_txt_generation` | `boolean` | `no` | `true` | Enable `generate_text_file()`. |
| `all` | `boolean` | `no` | `false` | Enable all file-generation functions. |

### Example

```yaml
agents:
  reporter:
    tools:
      - file_generation:
          output_directory: exports
          enable_pdf_generation: true
```

```python
generate_json_file({"status": "ok", "items": 3}, filename="summary.json")
generate_csv_file([{"name": "alpha", "value": 1}, {"name": "beta", "value": 2}], filename="data.csv")
generate_pdf_file("Quarterly summary", filename="report.pdf", title="Q1 Report")
generate_text_file("Plain text export", filename="notes.txt")
```

### Notes

- Filenames are auto-generated when omitted, and missing file extensions are appended automatically for the matching export type.
- `generate_json_file()` accepts dicts, lists, or strings, and plain strings are wrapped into JSON when they are not already valid JSON.
- Use a real `output_directory` if you want the artifact to remain on disk for later shell or file-tool access.

## [`visualization`]

`visualization` creates chart images with matplotlib and writes them to an output directory.

### What It Does

`visualization` exposes `create_bar_chart()`, `create_line_chart()`, `create_pie_chart()`, `create_scatter_plot()`, and `create_histogram()`.
The upstream toolkit switches matplotlib to the non-interactive `Agg` backend and auto-creates `output_dir` if it does not exist.
Each chart function accepts dict-like data, list-based data, or JSON strings, normalizes the data, saves a PNG image, and returns a JSON payload with the file path and status.
`visualization` defaults to primary execution.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `output_dir` | `text` | `no` | `"charts"` | Directory where generated chart images are saved. |
| `enable_create_bar_chart` | `boolean` | `no` | `true` | Enable `create_bar_chart()`. |
| `enable_create_line_chart` | `boolean` | `no` | `true` | Enable `create_line_chart()`. |
| `enable_create_pie_chart` | `boolean` | `no` | `true` | Enable `create_pie_chart()`. |
| `enable_create_scatter_plot` | `boolean` | `no` | `true` | Enable `create_scatter_plot()`. |
| `enable_create_histogram` | `boolean` | `no` | `true` | Enable `create_histogram()`. |
| `all` | `boolean` | `no` | `false` | Enable all chart functions. |

### Example

```yaml
agents:
  analyst:
    tools:
      - visualization:
          output_dir: charts
          enable_create_pie_chart: false
```

```python
create_bar_chart({"Mon": 12, "Tue": 18, "Wed": 9}, title="Requests per day")
create_line_chart({"Jan": 3, "Feb": 8, "Mar": 13}, title="Growth")
create_scatter_plot(
    x=[1, 2, 3],
    y=[2, 5, 7],
    title="Experiment results",
)
create_histogram([1, 1, 2, 3, 5, 8, 13], title="Value distribution")
```

### Notes

- The toolkit saves PNG files to disk immediately, so later tool calls can read or send those files.
- If you explicitly route `visualization` through workers, the chart files will be created in the worker-visible filesystem instead of the primary process filesystem.
- `matplotlib` must be importable in the runtime that executes the tool.

## [`sleep`]

`sleep` is a minimal delay utility for workflows that need an intentional pause between steps.

### What It Does

`sleep` exposes a single `sleep()` function that blocks for the requested number of seconds and then returns a confirmation string.
`sleep` defaults to primary execution.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_sleep` | `boolean` | `no` | `true` | Enable `sleep()`. |
| `all` | `boolean` | `no` | `false` | Enable all sleep functions. |

### Example

```yaml
agents:
  scheduler_helper:
    tools:
      - sleep
```

```python
sleep(5)
```

### Notes

- `sleep` is useful for deliberate polling loops or staged workflows, but it still ties up the runtime that executes it while the delay is in progress.
- If you explicitly route `sleep` through workers, the delay occurs in the worker runtime instead of the primary process.

## Related Docs

- [Tools Overview](index.md)
- [Per-Agent Tool Configuration](../configuration/agents.md#per-agent-tool-configuration)
- [Sandbox Proxy Isolation](../deployment/sandbox-proxy.md)
