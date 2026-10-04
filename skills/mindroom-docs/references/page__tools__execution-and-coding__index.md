# Execution & Coding

Use these tools to inspect and edit files, run shell commands or Python, manage Docker resources, do exact arithmetic, and generate files and charts.

<video controls playsinline preload="metadata" aria-label="An agent writes and runs an analysis script in its own workspace and posts the plot" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/1bc1dc4d-742f-490b-a177-3679ae412ec2#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/ac87d6b3-a82a-491d-ae5c-7d114f55539f#t=0.1" type="video/mp4">
</video>

## Tools On This Page

- [`file`] - Generic file reads, writes, listings, searches, and line-range edits.
- [`shell`] - Shell commands with background handles, environment passthrough, and PATH prepends.
- [`python`] - Python code execution and package installation.
- [`coding`] - Line-numbered reads, precise edits, grep, file discovery, and directory listing; the best default for code-editing agents.
- [`docker`] - Docker container, image, volume, and network management.
- [`calculator`] - Exact arithmetic without code execution.
- [`reasoning`] - `think` and `analyze` scratchpad steps.
- [`file_generation`] - JSON, CSV, PDF, DOCX, HTML, text, and source-code file exports.
- [`visualization`] - Matplotlib charts.
- [`sleep`] - Intentional pauses.

## Common Setup Notes

None of these tools needs credentials or dashboard setup; `docker` needs Docker daemon access in the runtime that runs it.
`docker`, `file_generation`, and `visualization` depend on the `docker`, `reportlab` and `python-docx`, and `matplotlib` packages, which install on first use unless [automatic dependency installation](https://docs.mindroom.chat/tools/#automatic-dependency-installation) is disabled.

### Where these tools run

`file`, `shell`, `python`, `coding`, and `docker` run in a worker by default when a worker backend or sandbox proxy is configured, and `reasoning` always runs in the primary process.
Whether the other tools here also run in a worker depends on the [execution mode](https://docs.mindroom.chat/deployment/sandbox-proxy/#execution-modes).
Change this with `defaults.worker_tools` or `agents.<name>.worker_tools`, as described in [Worker Routing](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-routing).
`worker_scope` decides how worker runtimes are shared between agents and users; see [Worker scopes](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-scopes).
Only a worker isolates `shell`, `python`, and `docker`, because they execute code and are never confined by the agent's [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) setting.

### Workspace and tool output files

When an agent has a workspace, it is the working directory (`base_dir`) of `file`, `shell`, `coding`, and worker-routed `python`, and generated files and charts are saved inside it.
`python` in the primary process resolves its file functions against the workspace, but the code it runs uses the process's current directory, so relative paths such as `open("data.csv")` may miss workspace files.
`base_dir` is managed by MindRoom and cannot be set inline in `config.yaml`; without a workspace, these tools use the process's current directory.

Agents with a workspace also get an optional `mindroom_output_path` argument on supported tools.
Set it to a workspace-relative file path to save the tool's full output to that file and return a short receipt to the model instead.
The path must name a file inside the workspace; absolute paths, `..`, paths that start with `~` or contain `$` or `%`, symlinks, and paths inside a `.git` directory are refused.
Without `mindroom_output_path`, outputs larger than `defaults.tool_output_auto_save_threshold_bytes` (integer, at least 1, default `51200`, which is 50 KiB) are saved under `mindroom_tool_outputs/` in the workspace, and the model receives the path, size, format, and a preview.
`MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES` sets the largest output either kind of save writes (default 64 MiB).

```yaml
defaults:
  tool_output_auto_save_threshold_bytes: 51200

agents:
  code:
    display_name: Code
    role: Edit code, run local checks, and export artifacts
    model: sonnet
    memory_backend: file
    tools:
      - coding
      - shell:
          extra_env_passthrough:
            - GITHUB_TOKEN
          shell_path_prepend:
            - /opt/custom/bin
      - file_generation:
          output_directory: exports
      - visualization:
          output_dir: charts
```

Here `file_generation` saves into `exports` and `visualization` into `charts`, both inside the agent workspace.

## [`file`]

`file` provides `save_file()`, `read_file()`, `delete_file()`, `list_files()`, `search_files()`, `search_content()`, `read_file_chunk()`, and `replace_file_chunk()`.
Paths follow the agent's [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access): `workspace` (the default) rejects paths outside the workspace, and `unrestricted` allows any path the tool's process can reach.
`save_file()`, `replace_file_chunk()`, and `delete_file()` refuse paths inside a `.git` directory.
`read_file()` refuses files over `max_file_length` characters or `max_file_lines` lines and asks for `read_file_chunk()` instead.
`search_files()` matches glob patterns; use `search_content()` to search inside text files.
Prefer [`coding`](#coding) for code-editing agents.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_save_file` | `boolean` | `no` | `true` | Enable `save_file()`. |
| `enable_read_file` | `boolean` | `no` | `true` | Enable `read_file()`. |
| `enable_delete_file` | `boolean` | `no` | `false` | Enable `delete_file()`; deletion is opt-in. |
| `enable_list_files` | `boolean` | `no` | `true` | Enable `list_files()`. |
| `enable_search_files` | `boolean` | `no` | `true` | Enable `search_files()`. |
| `enable_search_content` | `boolean` | `no` | `true` | Enable `search_content()`. |
| `enable_read_file_chunk` | `boolean` | `no` | `true` | Enable `read_file_chunk()`. |
| `enable_replace_file_chunk` | `boolean` | `no` | `true` | Enable `replace_file_chunk()`. |
| `expose_base_directory` | `boolean` | `no` | `false` | Include absolute file paths and `base_directory` in `search_files()` output. |
| `max_file_length` | `number` | `no` | `10000000` | Maximum character count for `read_file()`. |
| `max_file_lines` | `number` | `no` | `100000` | Maximum line count for `read_file()`. |
| `line_separator` | `text` | `no` | `"\n"` | Separator used by the chunk functions. |
| `exclude_patterns` | `string[]` | `no` | `null` | Fnmatch-style path-component patterns excluded from `search_content()`; `null` uses Agno's defaults and `[]` disables exclusions. |
| `all` | `boolean` | `no` | `false` | Enable every `file` function. |

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
read_file_chunk("src/app.py", 0, 80)
replace_file_chunk("docs/notes.md", 10, 12, "Updated text")
list_files(directory="src")
search_files("**/*.py")
search_content("TODO", directory="src")
save_file("temporary notes\n", "scratch/notes.txt")
```

## [`shell`]

`shell` provides `run_shell_command()`, `check_shell_command()`, and `kill_shell_command()`.
`run_shell_command()` accepts a shell command string or a list of argv strings.
Command strings run through non-login `bash -c`; pass `["bash", "-lc", "command"]` when login-shell startup files are needed, and use a multi-item argv list when exact argument boundaries matter.
If the command finishes within `timeout` seconds (default 120), the tool returns the last `tail` lines of stdout (default 100), capped at the last 50 KiB; on a non-zero exit, stderr is returned with stdout.
With `mindroom_output_path`, the complete output is saved to that file instead.

A command that exceeds `timeout` keeps running in the background, and the tool returns a `shell:...` handle.
Poll it with `check_shell_command(handle)` and stop it with `kill_shell_command(handle)`, or `kill_shell_command(handle, force=True)` to send SIGKILL.
A backgrounded command with `mindroom_output_path` saves its output when it finishes, and `check_shell_command()` then returns the file receipt.
Each runner keeps at most 16 backgrounded commands; more fail with `Error: Too many backgrounded processes (16/16). Kill or wait for existing ones before running more.`
Records of finished commands are cleared about 10 minutes after they finish.
For how stopping a response and worker restarts affect background commands, see [Stopping and background commands](https://docs.mindroom.chat/deployment/sandbox-proxy/#stopping-and-background-commands).

Worker-routed `shell` sees only a small default environment; which variables pass by default and which `extra_env_passthrough` never passes are listed in [Shell environment and PATH](https://docs.mindroom.chat/deployment/sandbox-proxy/#shell-environment-and-path).

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_run_shell_command` | `boolean` | `no` | `true` | Enable `run_shell_command()`, `check_shell_command()`, and `kill_shell_command()`. |
| `all` | `boolean` | `no` | `false` | Enable all shell functions. |
| `extra_env_passthrough` | `text` or `string[]` | `no` | `null` | Additional exported process environment variable names or glob patterns passed to shell commands; comma- or newline-separated in text form. It matches the exported process environment, not entries in the config's `.env` file. |
| `shell_path_prepend` | `text` or `string[]` | `no` | `null` | Directories prepended to `PATH` for shell commands only, with duplicates removed; comma- or newline-separated in text form. |

### Example

```yaml
agents:
  ops:
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
run_shell_command("git status --short", tail=50)
run_shell_command(["git", "status", "--short"], tail=50)
run_shell_command(["bash", "-lc", "sleep 300 && echo done"], timeout=2)
check_shell_command("shell:abcd1234")
kill_shell_command("shell:abcd1234")
```

### Per-workspace environment

An agent can set its own environment variables for worker-routed `shell` and `python`, such as `PATH` entries, npm prefixes, or package indexes, by exporting them from `.mindroom/worker-env.sh` in its workspace.
The hook's behavior, limits, and the variables it cannot override are described in [Workspace env hook](https://docs.mindroom.chat/deployment/sandbox-proxy/#workspace-env-hook-mindroomworker-envsh).

```bash
mkdir -p .mindroom .local/bin .cache/npm
cat > .mindroom/worker-env.sh <<'EOF'
export NPM_CONFIG_PREFIX="$PWD/.local"
export NPM_CONFIG_CACHE="$PWD/.cache/npm"
export PATH="$PWD/.local/bin:$PATH"
EOF
```

## [`python`]

`python` provides `run_python_code()`, `save_to_file_and_run()`, `run_python_file_return_variable()`, `pip_install_package()`, `uv_pip_install_package()`, `read_file()`, and `list_files()`.
It runs arbitrary code, so its file functions are never confined to the workspace and it always has unrestricted file access; isolate it with `worker_tools`.
Both install functions install into the interpreter running the tool, which is the worker's environment when `python` is worker-routed.
Worker-routed code can import modules saved in the workspace, but installed and standard-library modules take precedence over same-named workspace files, and user site-packages under the workspace home are not loaded.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `safe_globals` | `text` | `no` | `null` | Passed to Agno's `safe_globals` constructor argument; meant for programmatic wiring rather than hand-written YAML. |
| `safe_locals` | `text` | `no` | `null` | Passed to Agno's `safe_locals` constructor argument; meant for programmatic wiring rather than hand-written YAML. |

### Example

```yaml
agents:
  analyst:
    tools:
      - python
```

```python
run_python_code("total = sum(i * i for i in range(10))", variable_to_return="total")
save_to_file_and_run("scripts/demo.py", "result = 6 * 7", variable_to_return="result")
run_python_file_return_variable("scripts/demo.py", variable_to_return="result")
pip_install_package("rich")
```

## [`coding`]

`coding` provides `read_file()`, `edit_file()`, `write_file()`, `grep()`, `find_files()`, and `ls()`.
`read_file()` returns line-numbered output with pagination hints when a file is truncated.
`edit_file()` replaces text that must match exactly one location, tolerating whitespace and Unicode differences, and returns a unified diff; when a match is not unique, include more surrounding text in `old_text`.
`grep()` and `find_files()` skip hidden and gitignored paths, though `grep()` still searches a file named directly as its path; `ls()` shows dotfiles and marks directories with `/`.
Paths follow the agent's [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) like [`file`](#file), and `write_file()` and `edit_file()` refuse paths inside a `.git` directory.
`coding` has no configuration fields.

### Example

```yaml
agents:
  code:
    tools:
      - coding
```

```python
read_file("src/app.py", offset=1, limit=120)
grep("TODO", path="src")
find_files("**/*.md", path="docs")
edit_file("docs/example.md", "old text", "new text")
write_file("scratch/todo.txt", "first line\nsecond line\n")
ls("src")
```

## [`docker`]

`docker` manages containers with functions such as `list_containers()`, `run_container()`, `exec_in_container()`, `start_container()`, `stop_container()`, `remove_container()`, `get_container_logs()`, and `inspect_container()`.
It also manages images, volumes, and networks with functions such as `pull_image()`, `build_image()`, `tag_image()`, `list_volumes()`, `create_volume()`, `list_networks()`, and `connect_container_to_network()`.
Docker daemon access is privileged host control, so `docker` runs in a worker by default, and the runtime that runs it needs a reachable Docker daemon or socket.
`get_container_logs(stream=True)` returns a message asking for non-streaming mode instead of a live stream.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `include_tools` | `string[]` | `no` | `null` | Docker functions to expose; unset exposes all of them. |

### Example

```yaml
agents:
  platform:
    tools:
      - docker
```

```python
list_containers()
run_container("postgres:16", name="postgres", detach=True, environment={"POSTGRES_PASSWORD": "example"})
get_container_logs("postgres", tail=50)
```

## [`calculator`]

`calculator` provides `add()`, `subtract()`, `multiply()`, `divide()`, `exponentiate()`, `factorial()`, `is_prime()`, and `square_root()` for exact arithmetic without the risk of `python`.
Each function returns a small JSON result, and errors such as division by zero, negative factorials, and negative square roots come back as JSON errors.
`factorial()` accepts `n` up to 1558 and `is_prime()` accepts `n` up to 10**12; larger arguments return a JSON error.
`calculator` has no configuration fields.

```python
divide(22, 7)
factorial(6)
is_prime(97)
```

## [`reasoning`]

`reasoning` gives an agent a scratchpad for its own step-by-step reasoning rather than user-facing output.
`think()` records an intermediate thought and an optional next action.
`analyze()` records the result of a step and a `next_action` of `continue`, `validate`, or `final_answer`.
Steps are kept for the current run, so later steps see earlier ones.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_think` | `boolean` | `no` | `true` | Enable `think()`. |
| `enable_analyze` | `boolean` | `no` | `true` | Enable `analyze()`. |
| `add_instructions` | `boolean` | `no` | `false` | Add the toolkit's reasoning instructions to the prompt. |
| `add_few_shot` | `boolean` | `no` | `false` | Also add few-shot examples when instructions are added. |
| `instructions` | `text` | `no` | `null` | Replace the default reasoning instructions entirely. |
| `few_shot_examples` | `text` | `no` | `null` | Replace the built-in few-shot examples; used only when few-shot examples are added. |
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

## [`file_generation`]

`file_generation` provides `generate_json_file()`, `generate_csv_file()`, `generate_pdf_file()`, `generate_docx_file()`, `generate_html_file()`, `generate_text_file()`, and `generate_code_file()`.
Each function returns the generated file in its tool result.
Filenames are generated when omitted, and a missing extension is added for the export type.
`generate_json_file()` accepts dicts, lists, or strings, and wraps a string that is not valid JSON.
PDF and DOCX generation are disabled when `reportlab` or `python-docx` is unavailable, even if enabled in config.

Files are also saved to disk when `output_directory` is set or `save_files` is `true`.
Saved files go into `output_directory` inside the agent workspace, or the workspace root when only `save_files` is set, and overwrite files of the same name.
`output_directory` must be a relative path that stays inside the workspace.
Agents without a workspace cannot save generated files; the files exist only in the tool result.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `output_directory` | `text` | `no` | `null` | Directory inside the agent workspace where generated files are saved. |
| `save_files` | `boolean` | `no` | `false` | Save generated files; when `output_directory` is unset, they go to the workspace root. |
| `enable_json_generation` | `boolean` | `no` | `true` | Enable `generate_json_file()`. |
| `enable_csv_generation` | `boolean` | `no` | `true` | Enable `generate_csv_file()`. |
| `enable_pdf_generation` | `boolean` | `no` | `true` | Enable `generate_pdf_file()`. |
| `enable_docx_generation` | `boolean` | `no` | `true` | Enable `generate_docx_file()`. |
| `enable_txt_generation` | `boolean` | `no` | `true` | Enable `generate_text_file()`. |
| `enable_html_generation` | `boolean` | `no` | `true` | Enable `generate_html_file()`. |
| `enable_code_generation` | `boolean` | `no` | `true` | Enable `generate_code_file()`. |
| `all` | `boolean` | `no` | `false` | Enable all file-generation functions. |

### Example

```yaml
agents:
  reporter:
    tools:
      - file_generation:
          output_directory: exports
```

```python
generate_csv_file([{"name": "alpha", "value": 1}, {"name": "beta", "value": 2}], filename="data.csv")
generate_pdf_file("Quarterly summary", filename="report.pdf", title="Q1 Report")
generate_docx_file("Quarterly summary", filename="report.docx", title="Q1 Report")
```

## [`visualization`]

`visualization` provides `create_bar_chart()`, `create_line_chart()`, `create_pie_chart()`, `create_scatter_plot()`, and `create_histogram()`.
Bar, line, and pie charts accept a dict, a list of dicts, or a JSON string; scatter plots take x and y lists, and histograms take a list of numbers.
Each function saves a PNG in `output_dir` inside the agent workspace and returns the file path.
A chart's `filename` must be a plain file name; when omitted, the chart is named after its type and numbered, such as `bar_chart_3.png`.
Charts overwrite files of the same name, and `output_dir` is created when missing.
Agents without a workspace cannot save charts.
Because charts are in the workspace, `attachments` and `matrix_message` can send them under the default `file_access: workspace`.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `output_dir` | `text` | `no` | `"charts"` | Directory inside the agent workspace where charts are saved; absolute paths and `..` are rejected. |
| `enable_create_bar_chart` | `boolean` | `no` | `true` | Enable `create_bar_chart()`. |
| `enable_create_line_chart` | `boolean` | `no` | `true` | Enable `create_line_chart()`. |
| `enable_create_pie_chart` | `boolean` | `no` | `true` | Enable `create_pie_chart()`. |
| `enable_create_scatter_plot` | `boolean` | `no` | `true` | Enable `create_scatter_plot()`. |
| `enable_create_histogram` | `boolean` | `no` | `true` | Enable `create_histogram()`. |
| `all` | `boolean` | `no` | `false` | Enable all chart functions. |

### Example

```python
create_bar_chart({"Mon": 12, "Tue": 18, "Wed": 9}, title="Requests per day")
create_scatter_plot(x=[1, 2, 3], y=[2, 5, 7], title="Experiment results")
create_histogram([1, 1, 2, 3, 5, 8, 13], title="Value distribution")
```

## [`sleep`]

`sleep()` waits for 0 to 300 seconds and returns a confirmation; other durations return an error without waiting.
The response stays open until the delay ends or the response is stopped.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_sleep` | `boolean` | `no` | `true` | Enable `sleep()`. |
| `all` | `boolean` | `no` | `false` | Enable all sleep functions. |

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration)
- [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/)
