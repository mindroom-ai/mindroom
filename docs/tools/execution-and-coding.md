---
icon: lucide/code
---

# Execution & Coding

Use these tools to inspect and edit files, run shell commands or Python, manage Docker resources, do exact arithmetic, and generate files and charts.

<video controls playsinline preload="metadata" aria-label="An agent writes and runs an analysis script in its own workspace and posts the plot" style="width: 100%" poster="https://github.com/user-attachments/assets/524efcc4-9b4c-4ee1-85c3-bca8dc13e1f8" data-poster-light="https://github.com/user-attachments/assets/524efcc4-9b4c-4ee1-85c3-bca8dc13e1f8" data-poster-dark="https://github.com/user-attachments/assets/e62497d7-3133-4abe-afc2-ec3db2889552">
  <source src="https://github.com/user-attachments/assets/36d029f7-b67d-46c7-ae38-f76517dcd354" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/1e73d587-9a0e-44c3-a66d-8b6631cdbf61" type="video/mp4">
</video>

## Tools On This Page

- [`filesystem`] - Line-numbered reads, precise edits, writes, grep, file discovery, directory listing, and opt-in deletes; `file` and `coding` are other names for it.
- [`shell`] - Shell commands with time limits, environment passthrough, and PATH prepends.
- [`terminal`] - Persistent terminals beside the agent's shell commands, which people in the room can watch and type in when the agent's config allows it.
- [`temporary_secrets`] - Ask the user for a password, token, or code that the agent's shell commands receive and the agent never sees.
- [`shared_directories`] - Directories an agent creates and invites other agents to read.
- [`docker`] - Docker container, image, volume, and network management.
- [`calculator`] - Exact arithmetic without code execution.
- [`reasoning`] - `think` and `analyze` scratchpad steps.
- [`file_generation`] - JSON, CSV, PDF, DOCX, HTML, text, and source-code file exports.
- [`visualization`] - Matplotlib charts.
- [`sleep`] - Intentional pauses.

## Common Setup Notes

None of these tools needs credentials or dashboard setup; `docker` needs Docker daemon access in the runtime that runs it.
`docker`, `file_generation`, and `visualization` depend on the `docker`, `reportlab` and `python-docx`, and `matplotlib` packages, which install on first use unless [automatic dependency installation](index.md#automatic-dependency-installation) is disabled.

### Where these tools run

`filesystem`, `shell` and `docker` run in a worker by default when a worker backend or sandbox proxy is configured, and `reasoning` always runs in the primary process.
Whether the other tools here also run in a worker depends on the [execution mode](../deployment/sandbox-proxy.md#execution-modes).
Change this with `defaults.worker_tools` or `agents.<name>.worker_tools`, as described in [Worker Routing](../deployment/sandbox-proxy.md#worker-routing).
`worker_scope` decides how worker runtimes are shared between agents and users; see [Worker scopes](../deployment/sandbox-proxy.md#worker-scopes).
With [Agent Keys](../deployment/egress-broker.md) on, `shell` calls of an agent with the [`egress_credentials` tool](../deployment/egress-broker.md#agent-tool) can use stored API keys and connected accounts that the worker never sees.
Only a worker isolates `shell` and `docker`, because they execute code and are never confined by the agent's [`file_access`](../architecture/security-posture.md#file-access) setting.

### Workspace and tool output files

Every agent has a workspace: `agents/<agent>/workspace/` in the storage directory, or the requester's private root for a [`private`](../configuration/agents.md#private-instances) agent.
It is the working directory (`base_dir`) of `filesystem` and `shell`, and generated files and charts are saved inside it.
`base_dir` is managed by MindRoom and cannot be set inline in `config.yaml`; a [headless run](../headless.md#files) can point it at a directory the caller chooses.

Supported tools also take an optional `mindroom_output_path` argument.
Set it to a workspace-relative file path to save the tool's full output to that file and return a short receipt to the model instead.
The path must name a file inside the workspace; absolute paths, `..`, paths that start with `~` or contain `$` or `%`, symlinks, and paths inside a `.git` directory are refused.
Without `mindroom_output_path`, outputs larger than `defaults.tool_output_auto_save_threshold_bytes` (integer, at least 1, default `51200`, which is 50 KiB) are saved under `mindroom_tool_outputs/` in the workspace, and the model receives the path, size, format, a preview, and a note to read the file in parts, because reading it whole would only save it again.
`MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES` sets the largest output either kind of save writes (default 64 MiB).

```yaml
defaults:
  tool_output_auto_save_threshold_bytes: 51200

agents:
  code:
    display_name: Code
    role: Edit code, run local checks, and export artifacts
    model: sonnet
    tools:
      - filesystem
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

## [`filesystem`]

<a id="file"></a><a id="coding"></a>
`filesystem` provides `read_file()`, `edit_file()`, `write_file()`, `grep()`, `find_files()`, and `ls()`, and `delete_file()` when `enable_delete_file` is on.
The tool names `file` and `coding` select it too wherever a tool name is written anew, such as `tools`, `worker_tools`, `MINDROOM_SANDBOX_PROXY_TOOLS`, a new contact's tools, and subagent tools, and config saved through the dashboard or API writes `filesystem`.
An agent's `tools` and the `defaults.tools` it takes may name it by more than one of the three names only when none of those entries has settings and their `defer` and `initial` flags agree; it then stands where the first one stood, and otherwise the config fails to load, naming both entries.
What was saved under `file` or `coding` before the two became one toolkit is not carried over; see [Upgrading to the `filesystem` toolkit](../deployment/upgrades.md#upgrading-to-the-filesystem-toolkit).
`read_file()` returns line-numbered output with pagination hints when a file is truncated, and names an image it cannot decode instead of returning its bytes, pointing to `view_file()` when the agent has it.
`edit_file()` applies a list of `edits` to one file, each an `old_text` copied from the file and its `new_text`.
Every `old_text` matches the file as it was before the call, not the results of the other edits, and must match exactly one place unless its edit sets `replace_all`, which replaces every non-overlapping match from left to right, as Python's `str.replace` does; when it has no exact match, a match that differs only in whitespace and quotes is used.
Edits must not overlap, so an edit must not also target text that a `replace_all` edit's `old_text` matches.
When an edit fails validation, none is written: the error names the edit, lists the lines of an `old_text` that matches several places, and, for one that matches none, shows up to three distinct lines that come close, if any do.
A failure while writing the file is reported as a write error instead.
On success it returns the lines each edit changed in the new file, numbered as `read_file()` numbers them, not a diff; a match whose `new_text` equals the text it replaces is left out of those lines, though the edit still counts.
In a file whose lines all end in CRLF, `old_text` and `new_text` may use LF or CRLF and the file keeps CRLF, unless the file or an edit holds a lone CR; then, as with mixed endings, the file is matched and edited with its line endings as they are.
A byte order mark stays in the file once, also when an edit that starts at it copies it into `old_text` or `new_text`; any other U+FEFF is ordinary text.
In the primary, `edit_file()`, `write_file()`, and `delete_file()` calls on the same resolved path run one after another, so they never interleave; a later `write_file()` still replaces the whole file, an earlier edit included.
In a worker, calls serialize only when they lock the same stable, readable directory; one that waits too long for that lock returns a busy error without changing the file, and a directory the agent may enter but not read cannot hold the lock, so calls there run unlocked.
Primary and worker calls do not serialize with each other, with another primary, or with other writers such as shell commands.
`grep()` and `find_files()` skip gitignored paths and hidden paths below the directory they search, so naming a dot directory as the path searches it, and `grep()` still searches a file named directly as its path; `ls()` shows dotfiles and marks directories with `/`.
Under `file_access: workspace`, `grep()` skips a file over 64 MiB, which `read_file()` refuses to read.
`delete_file()` removes a file or an empty directory.
Paths follow the agent's [`file_access`](../architecture/security-posture.md#file-access): `workspace` (the default) rejects paths outside the workspace and the agent's [shared directories](#shared_directories), and `unrestricted` allows any path the tool's process can reach.
`write_file()`, `edit_file()`, and `delete_file()` refuse paths inside a `.git` directory.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_delete_file` | `boolean` | `no` | `false` | Offer `delete_file()`; deletion is opt-in. |

Turn functions off with the toolkit filters every tool takes, for example `exclude_tools: [write_file, edit_file]` for an agent that only reads.

### Example

```yaml
agents:
  code:
    tools:
      - filesystem
  editor:
    tools:
      - filesystem:
          enable_delete_file: true
```

```python
read_file("src/app.py", offset=1, limit=120)
grep("TODO", path="src")
find_files("**/*.md", path="docs")
edit_file(
    "docs/example.md",
    edits=[
        {"old_text": "old text", "new_text": "new text"},
        {"old_text": "draft", "new_text": "final", "replace_all": True},
    ],
)
write_file("scratch/todo.txt", "first line\nsecond line\n")
ls("src")
delete_file("scratch/todo.txt")
```

## [`shell`]

`shell` provides `run_shell_command()`, which accepts a shell command string or a list of argv strings.
It runs commands only in a worker unless [`defaults.allow_shell_without_worker: true`](../configuration/agents.md#defaults) lets them run in the MindRoom process; otherwise each call there is refused.
Command strings run through non-login `bash -c`; pass `["bash", "-lc", "command"]` when login-shell startup files are needed, and use a multi-item argv list when exact argument boundaries matter.
The tool returns the last `tail` lines of stdout (default 100), then the last `tail` lines of any stderr under `Stderr:`, each capped at the last 50 KiB; on a non-zero exit, an `Exit code <code>` line comes first, followed by stdout under `Output:` and all of stderr under `Stderr:`.
If the result is auto-saved, read the saved file rather than relying on the preview for the last lines.
When `tail` or the cap leaves lines out of a successful result, it starts with a notice for each stream that lost lines, such as `[Showing the last 100 of 230 lines. Pass a larger tail, or narrow the command.]` or `[Showing the last 100 of 230 stderr lines. ...]`.
Blank lines count as lines in a notice and in the tail; output shown whole loses only its trailing newlines, and stderr of only blank lines still gets its `Stderr:` heading.
A result that leads with a status line keeps it first and puts the notice at the start of the stream it describes: `Exit code <code>` on a failure, `Stopped after <timeout>s.` at the timeout, and `Status: FINISHED` or `Status: RUNNING` when a background command is checked.
The command runs in the agent's workspace, or in its workdir, and the result holds only the command's output.
With `mindroom_output_path`, the complete output is saved to that file instead, and the receipt includes `exit_code` whenever the command exits non-zero or is killed, such as by the OOM killer, even when no file is saved; a command stopped at its `timeout` reports the stop instead.
A successful command saves its stdout alone, and when it also printed to stderr, the receipt gives its size as `stderr_bytes`; add `2>&1` to the command to save that text too.

By default a shell call waits at most 120 seconds in the foreground.
A command with `timeout` <= 120 that is still running after `timeout` seconds is stopped together with the processes it started, and the tool returns its output so far after `Stopped after <timeout>s.`.
When `timeout` is omitted or > 120 and the command is still running after 120 seconds, a call that runs as a [background job](agent-orchestration.md#background-jobs) (with `background_after` unset) returns a job handle while the command continues until completion, its `timeout`, or job cancellation; any other turn-scoped call (including subagent calls, minimal-mode turns, and turns with background jobs off) moves the command to the background, streams ongoing stdout and stderr into a bounded live log file under `.mindroom/shell/` ending with the exit status, returns its PID, log path, and wait/stop commands, and stops the process group automatically when `timeout` (if set) expires or when the turn ends.
A managed top-level job still saves `mindroom_output_path` when it completes; a command stopped at its timeout or backgrounded in the current turn saves no file.
While a job's command runs, `job(action="list")` shows the last lines of its output, and cancelling the job stops the command.
A command that exits non-zero or stops at its timeout returns an error result, so its job ends `failed`; its summary in `job(action="list")`, the first 500 characters of that result, includes the `Exit code <code>` or `Stopped after <timeout>s.` line (or, with `mindroom_output_path`, the output-file receipt), and `job(action="wait")` / `job(action="collect")` still deliver that full output.
A process started with `nohup cmd > out.log 2>&1 &` keeps running after its command ends on its own, but not after a stop; only a process started in its own session, such as with `setsid`, survives a stop.
For how stopping a response and worker restarts affect running commands, see [Stopping and background commands](../deployment/sandbox-proxy.md#stopping-and-background-commands).

Worker-routed `shell` sees only a small default environment; which variables pass by default and which `extra_env_passthrough` never passes are listed in [Shell environment and PATH](../deployment/sandbox-proxy.md#shell-environment-and-path).

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_run_shell_command` | `boolean` | `no` | `true` | Enable `run_shell_command()`. |
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
run_shell_command(["bash", "-lc", "make test"], timeout=900)
run_shell_command("nohup npm run dev > dev.log 2>&1 &")
```

### Linux resource isolation

On Linux, MindRoom starts shell commands at `SCHED_IDLE` CPU priority (`chrt -i 0`, falling back to moving the process tree after spawn) and idle disk I/O priority (`ionice -c 3`, which takes effect when the disk scheduler honors I/O priorities such as BFQ, and fails open when unavailable).

Deployments that route `shell` to Docker or Kubernetes workers already isolate commands in their worker containers.
When running `shell` directly in the MindRoom process on a Linux systemd host (`defaults.allow_shell_without_worker: true`), spawned commands inherit MindRoom's own service cgroup by default, so heavy builds or test suites can compete with MindRoom for RAM and swap.

To move local shell commands into their own bounded cgroup, set `MINDROOM_SHELL_SYSTEMD_SLICE` to a systemd user slice name ending in `.slice` (for example `mindroom-shells.slice`).
MindRoom probes `systemd-run --user --scope` together with `env` once per `(slice, XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS)` and caches the result for the process lifetime, failing open when unavailable (including on older systemd releases without `--expand-environment=no`).
When active, each command runs with a fixed `--description=MindRoom shell` so raw arguments are not written to the systemd user journal, `--expand-environment=no` so literal `$$` and `$VAR` arguments pass through unchanged, and `env` restoration so `systemd-run` uses MindRoom's user bus while the command sees the caller's original values or omissions for those two bus variables.

Minimal systemd setup for a MindRoom system service running as `<user>` (uid `<uid>`):

1. Enable lingering so `user@<uid>.service` starts at boot (`sudo loginctl enable-linger <user>`), and delegate controllers to it (for example in `/etc/systemd/system/user@<uid>.service.d/delegate.conf`):

   ```ini
   [Service]
   Delegate=cpu cpuset io memory pids
   ```

2. Create the user slice (`~/.config/systemd/user/mindroom-shells.slice`) with limits sized to the host:

   ```ini
   [Unit]
   Description=MindRoom agent shell subprocesses

   [Slice]
   MemoryHigh=50%
   MemoryMax=60%
   MemorySwapMax=4G
   CPUWeight=1
   IOWeight=1
   ```

   You can also set `AllowedCPUs=` to reserve dedicated threads for MindRoom and the host.

3. Point MindRoom's service at the user manager and slice, and tear the slice down on service stop so detached shells do not outlive a restart:

   ```ini
   [Unit]
   After=user@<uid>.service
   Wants=user@<uid>.service

   [Service]
   Environment=XDG_RUNTIME_DIR=/run/user/<uid>
   Environment=MINDROOM_SHELL_SYSTEMD_SLICE=mindroom-shells.slice
   ExecStopPost=-/usr/bin/systemctl --user stop mindroom-shells.slice
   MemoryMin=4G
   MemoryLow=6G
   ```

   In cgroup v2, `MemoryMin` and `MemoryLow` are bounded by ancestor cgroups, so set matching values on `system.slice` (and on the container cgroup when running inside LXC/Incus).

### Named workers

On the Kubernetes worker backend, an agent's shell commands can run in extra workers that the agent names, so one reply runs several isolated environments side by side, such as end-to-end test stacks that listen on the same ports, or the shards of a test suite or a build.
Set [`named_workers`](../configuration/agents.md#configuration-options) on the agent to the most named workers it may have at once for one requester; MindRoom then offers `worker`, `resource_profile`, `sandbox`, `mount_dir`, and `mount_read_only` in its `run_shell_command`.
The agent's workers must be per requester and agent: set `worker_scope: user_agent`, or make it a [private agent](../configuration/agents.md#private-instances) with `private.per: user_agent`.
The runtime must also run idle-worker cleanup, as for [dedicated subagent workers](agent-orchestration.md#dedicated-workers).

```yaml
agents:
  code:
    display_name: Code
    tools: [shell]
    worker_tools: [shell]
    worker_scope: user_agent
    named_workers: 8
```

```python
run_shell_command("bash e2e/shard.sh 0 2", worker="e2e-0", resource_profile="large", timeout=3600, background_after=0)
run_shell_command("bash e2e/shard.sh 1 2", worker="e2e-1", resource_profile="large", timeout=3600, background_after=0)
```

- `worker` is a name of 1 to 32 lowercase letters, digits, or hyphens that starts with a letter or digit.
  The first call with a name starts a worker, and later calls of the same reply with that name, including its [background jobs](agent-orchestration.md#background-jobs) and its [`mindroom-agent`](agent-cli.md#run-commands-in-named-workers) calls, run in it.
  Another reply's calls with the same name get a worker of their own.
- `resource_profile` is `small`, `standard`, or `large`, the [background script profiles](../deployment/kubernetes.md#worker-settings), and sizes the worker when its name is first used; omitting it gives the worker the normal worker resources.
  A later call that asks for another size fails.
  Commands in Kubernetes workers (not Docker workers) get the `sandbox-runner` container's CPU limit in `MINDROOM_WORKER_CPUS` and derived `OMP_NUM_THREADS`, `PYTHON_CPU_COUNT`, and `PYTEST_XDIST_AUTO_NUM_WORKERS` so GNU `nproc` (BusyBox `nproc` ignores `OMP_NUM_THREADS`) and parallel tools match the worker container; `free` still reports the host node, so read `MINDROOM_WORKER_MEMORY_BYTES` or cgroup v2 `/sys/fs/cgroup/memory.max` for the container memory limit.
- The result starts with `[worker: <name>]`, or `[worker: <name>, started]` on the first call to a Deployment or a start MindRoom recorded.
  Kubernetes may replace a pod or restart its container without changing the Deployment. The marker does not report those restarts.
- A named worker has its own processes, network ports, and `/tmp`.
  It sees the same workspace, shared directories, and knowledge as the agent's normal worker, with the same leased tool settings and shared credentials, but not the packages installed into the normal worker's environment or its package caches.
- Workers share the workspace, so give each one its own output directory, and its own git worktree when it changes a checkout, and build or install what all of them need once before starting them.
- Set `sandbox=true` (empty ephemeral scratch directory only) or `mount_dir` (one existing workspace-relative subdirectory without symlinks, optionally with `mount_read_only=true`; the workspace root `"."` and dot-prefixed segments such as `.mindroom`, `.config`, or `.git` are refused) on the first call for a name to isolate that worker. `mount_dir` always enables sandbox mode.
  A sandboxed named worker mounts no other workspace files, no shared directories, no additional knowledge bases, and no ordinary-worker `extra_containers` or `extra_volumes`; does not source `.mindroom/worker-env.sh`; and omits mirrored shared credentials, credential leases, Agent Keys broker variables, per-call environment secrets and `extra_env_passthrough`, primary manifest secrets, and `MINDROOM_AGENT_CLI_TOKEN`. Its pod and Deployment carry the label `mindroom.ai/worker-sandbox: "true"` for optional Kubernetes `NetworkPolicy` egress rules.
  Note that a writable `mount_dir` still allows repo-local files such as `.git/hooks` to be modified if the normal worker later runs `git` there; use `mount_read_only=true` or empty-scratch `sandbox=true` for untrusted code.
  `mount_dir` requires an inode-preserving container runtime and storage driver and is refused when `workers.kubernetes.runtimeClassName` is set (runtimes such as gVisor do not preserve host inode numbers for mount verification); empty-scratch `sandbox=true` without `mount_dir` continues to work with `runtimeClassName`.
  Its `HOME` and `XDG_*_HOME` stay on the writable worker state root while `cwd` and `MINDROOM_AGENT_WORKSPACE` point to the mounted directory (or ephemeral scratch). Explicit `mindroom_output_path` is not supported in sandboxed named workers; auto-saved output on a writable `mount_dir` returns an agent-workspace-relative path (`<mount_dir>/...`).
  Sandbox settings lock on the first admitted call alongside `resource_profile`; follow-up calls omitting them reuse the locked sandbox and still strip all credentials, secrets, and CLI grants, while conflicting sandbox options fail.

Named workers are removed, with their processes and package caches, when the reply and its background jobs end.
A named worker in which no command has run for 10 minutes is removed sooner, and every named worker is removed 4 hours after it started, whatever runs in it; [worker settings](../deployment/kubernetes.md#worker-settings) lists both limits.
Run long work as the call's own command rather than detached with `nohup`, and keep results in the workspace.
Stopping the reply or cancelling a job stops its command in the named worker, as in the normal worker.
A MindRoom restart ends a reply's use of its named workers: the first cleanup pass after the restart removes them, and a later reply's calls start new ones.
If a named worker crashes, the call that ran in it fails, and the next call with its name starts a new one.
A reply that pauses for a [tool approval](../tool-approval.md) gives up its named workers unless one of its background jobs still runs, and its calls after the approval start new ones, so an agent whose shell commands need approval gets a new worker for each command.

A named worker counts against `named_workers` for its requester, and against `MINDROOM_KUBERNETES_NAMED_WORKERS_MAX` for the whole deployment, from the call that starts it until it is removed, so also while it starts, sits idle, or has failed.
Set `MINDROOM_KUBERNETES_NAMED_WORKERS_MAX=0` to disable named workers for the whole deployment.
A call that would start one more than either limit allows fails instead of waiting. Reusing a worker this reply started does not recheck the limits while the worker is starting or its Kubernetes Deployment exists and is not being removed. If idle cleanup removes it, starting it again is subject to both limits.
Only calls of a reply in a Matrix conversation can name a worker, so [background scripts](background-scripts.md) and [OpenAI-compatible API](../openai-api.md) requests cannot.
[Approved egress](../deployment/approved-egress.md#requesting-access) grants do not reach named workers.

Errors:

- `Named workers are off for this agent; named_workers in its config allows them.` - set `named_workers` on the agent.
- `Named workers need an agent whose workers are per requester and agent: worker_scope: user_agent, or private.per: user_agent.` - set one of the two on the agent.
- `Named workers need the kubernetes worker backend and idle-worker cleanup, which the API server runs when MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS is above 0.` - use the Kubernetes worker backend, run the API server, and set the cleanup interval.
- `Cannot start worker '<name>': this agent already has <n> of the <n> named workers it may have for you.` - run the command in a worker this reply started, wait until an idle one is removed, or raise `named_workers`.
- `Cannot start worker '<name>': this deployment already has its limit of <n> named workers.` - try again later, or raise `MINDROOM_KUBERNETES_NAMED_WORKERS_MAX`.
- `Worker '<name>' was started with resource_profile <profile> in this reply; omit resource_profile, or name another worker.` - a name keeps the size its first call gave it.
- `A named worker belongs to a reply in a conversation, and this call runs outside one, or after its reply and that reply's background jobs ended.` - the call came from a background script, an API request, or a job that outlived a restart.

### Per-workspace environment

An agent can set its own environment variables for worker-routed `shell`, such as `PATH` entries, npm prefixes, or package indexes, by exporting them from `.mindroom/worker-env.sh` in its workspace.
The hook's behavior, limits, and the variables it cannot override are described in [Workspace env hook](../deployment/sandbox-proxy.md#workspace-env-hook-mindroomworker-envsh).

```bash
mkdir -p .mindroom .local/bin .cache/npm
cat > .mindroom/worker-env.sh <<'EOF'
export NPM_CONFIG_PREFIX="$PWD/.local"
export NPM_CONFIG_CACHE="$PWD/.cache/npm"
export PATH="$PWD/.local/bin:$PATH"
EOF
```

## [`terminal`]

`terminal` gives an agent persistent terminals: interactive Bash shells in its workspace that keep running between calls, which people in the room can watch live and type in from a chat client that shows terminals, as far as the agent's [`terminals`](../configuration/agents.md) setting allows.
Use it for work people should see or that keeps state between commands, such as a dev server, a REPL, an interactive program, or a long build.

- `terminal_open(cwd)` opens a terminal in `cwd`, a folder of the workspace, by default the workspace itself.
- `terminal_run(session_id, command, timeout_s)` types one command line while the shell waits at an empty prompt, waits up to `timeout_s` seconds (default 30, at most 600) for its next prompt, and returns the output and exit status; a command still running then keeps running.
- `terminal_send(session_id, keys)` types text and named keys, such as `<enter>`, `<tab>`, `<up>`, and `<ctrl-c>`, without waiting for a command; `<ctrl-c>` goes on its own, since the terminal may drop keys typed right after it.
- `terminal_read(session_id)` returns the terminal's recent output as text.
- `terminal_list()` lists the workspace's terminals, including the ones people opened.

An agent has terminals only when it also has [`shell`] with `run_shell_command` and that shell runs in a worker; otherwise it is offered none of these functions.
A terminal runs in that worker with the environment and workspace of the agent's shell commands, except that it never gets the response's `mindroom-agent` CLI access, because it outlives the response.
It also starts with `INPUTRC=/dev/null`, so it and the readline programs started in it keep their built-in key bindings and read no readline settings, such as a `~/.inputrc` in the workspace.
Everyone whose turns reach the same agent workspace in the same worker shares its terminals.
With [`terminals: type`](../configuration/agents.md), people who may post messages in the room type in them where the worker mounts the agent's workspace, a dedicated Docker or Kubernetes worker or a `static_runner` with [`MINDROOM_STATIC_RUNNER_AGENT_STORAGE`](../deployment/sandbox-proxy.md#filesystem-isolation-and-agent-data); everyone else who may use the agent watches.
After keys typed while a command runs, such as an answer to its prompt, `terminal_run` refuses until the agent sends `<ctrl-c>` at the prompt that follows, and inside a shell started in the terminal, such as `bash` or `nix-shell`, it refuses until that shell exits, so the agent types into it with `terminal_send`.
A terminal ends 30 minutes after its last input while nobody watches, and when its worker stops.

An agent types in terminals only while its `run_shell_command` calls never ask for approval: the first [approval rule](../tool-approval.md) that matches `run_shell_command` is `auto_approve`, or no rule matches and `tool_approval.default` is `auto_approve`; a rule with an approval script counts as one that may ask.
Otherwise the agent is offered only `terminal_read` and `terminal_list`, and its instructions tell it to run commands with `run_shell_command`; people the agent's `terminals` setting lets type still type in the terminals.
The rules can't judge typing into a terminal, because what a line runs there depends on what ran in it before, such as aliases, functions, shell options, and programs reading the keys.

```yaml
agents:
  ops:
    tools:
      - shell
      - terminal
    terminals: type
```

## [`temporary_secrets`]

`temporary_secrets` lets an agent ask for a secret that a shell command needs and that the agent must not see, such as a `sudo` password, a token for a host without an [Agent Keys](../deployment/egress-broker.md) service, a passphrase, or a pairing code.
It provides `request_temporary_secret(name, reason)`, where `name` is the environment variable the commands should receive and `reason` is what the user reads on the card.
The agent can also give a `label` that says what the secret is, the `url` of a page where the user gets or creates it, and a `placeholder` for the empty field.

- MindRoom Chat shows the requester a card with the reason, the variable, and a masked field.
  Other people in the thread see who the card waits for.
- A `label` becomes the card's title and a `placeholder` the hint in the empty field, each cut to one line of 80 characters.
  A `url` becomes a button that names the page's host and opens it in the browser, and it must be an `http` or `https` link with a host, no `user:password@` part, and no spaces.
  The agent chooses the link, so the user should check the host on the button before signing in there.
- The requester chooses how long MindRoom keeps the value: 15 minutes in the memory of the MindRoom process, which is the default, or 24 hours, for which MindRoom also writes it to a file that only its own account can read, under `temporary_secrets/` in the storage directory.
  A restart forgets a value kept in memory and reads back one kept in a file.
  The file is deleted at the first tool call or answer after the value expired.
- Chat sends the value to this MindRoom's API with the user's client session, so it never goes through Matrix or the model.
- While it is kept, the value is the environment variable `name` of that agent's `shell` commands (`run_shell_command`) in the same room and thread, in turns of the same requester, whether they run in the MindRoom process or in a worker.
  MindRoom gives it to no other agent, conversation, or person's turn, and not to a [`terminal`], whose shell would keep it past its lifetime.
  In a worker that several people's turns share, another person's command can still read the environment of a command while it runs, and a process that a command leaves running keeps the variable for as long as it runs.
- The tool returns `provided` with the minutes the variable stays set, `declined`, or `expired` when no answer came in 15 minutes.
  It waits, stops, and continues as a [background job](agent-orchestration.md#background-jobs) the way [`request_secret`](../deployment/egress-broker.md#asking-in-chat) does, and needs the same access for Chat: its origin in `MINDROOM_COMPUTER_ALLOWED_ORIGINS` and a homeserver that binds OpenID tokens to this deployment.

Unlike an Agent Keys key, a temporary secret reaches the agent's commands, so a command can print it or send it elsewhere.
MindRoom tells the agent to use the variable only inside the command that needs it, and replaces exact copies of the value in tool results and in the output a running command shows, before the model or the room reads them, but neither stops an agent that works around them.
What a command prints can also end up in the agent's workspace, in a file the command writes or in the file MindRoom saves a large result or a `mindroom_output_path` result to; those files hold the output as the command printed it.
Give the tool to agents you would trust with the secret, and prefer an [Agent Keys](../deployment/egress-broker.md) service for an API key that a service's hosts take in a header.

```yaml
agents:
  ops:
    tools:
      - shell
      - temporary_secrets
```

## [`shared_directories`]

`shared_directories` lets an agent create a directory, write files into it, and invite other agents to read them.
It provides `create_shared_directory(name)`, `invite_to_shared_directory(name, agent_name)`, `remove_from_shared_directory(name, agent_name)`, `delete_shared_directory(name)`, `list_shared_directories()`, `accept_shared_directory(owner, name)`, and `decline_shared_directory(owner, name)`, and has no configuration fields.

### Setup

1. Run tools in dedicated Docker or Kubernetes workers.
   Set the environment variable `MINDROOM_WORKER_BACKEND=docker` as in [dedicated Docker workers](../deployment/sandbox-proxy.md#host-machine-dedicated-docker-workers-mindroom_worker_backenddocker), or the runtime chart's Helm value `workers.backend: kubernetes` as in [Kubernetes dedicated worker mode](../deployment/kubernetes.md#dedicated-worker-mode).
   Sharing also works without workers and with the Kubernetes `static_runner` sidecar, with weaker limits; see [Access by deployment](#access-by-deployment).
2. Give the sharing agent `shared_directories` and a tool that writes files, such as `shell` or `filesystem`.
3. Give each reading agent `shell` or `filesystem`.
   It also needs `shared_directories` to accept invitations from users who cannot use it, as in [Sharing between personal agents](#sharing-between-personal-agents).
4. When the user who asks for the invitation can use the reading agent outside the current room, the invitation applies at once, as described under [Rules](#rules); in the example below, every member of `lobby` can.

```yaml
agents:
  researcher:
    display_name: Researcher
    rooms: [lobby]
    tools:
      - shell
      - shared_directories
  writer:
    display_name: Writer
    rooms: [lobby]
    tools:
      - filesystem
```

Then ask `researcher` in `lobby`, for example: "Save your notes in a shared directory called report and invite writer."
It calls `create_shared_directory("report")` and `invite_to_shared_directory("report", "writer")`, and from then on both agents see the directory's path in their prompts.
`list_shared_directories()` shows each directory an agent owns or joined, with its path and access, the pending invitations of each directory it owns, and the invitations waiting for it.

### Sharing between personal agents

When each person has an agent only they may use, an invitation from one person waits until the other accepts it through their own agent.

```yaml
agents:
  ann_assistant:
    display_name: Ann's Assistant
    access:
      users: ["@ann:example.org"]
    tools: [shell, shared_directories]
  ben_assistant:
    display_name: Ben's Assistant
    access:
      users: ["@ben:example.org"]
    tools: [shell, shared_directories]
  team_helper:
    display_name: Team Helper
    access:
      users: ["@ann:example.org", "@ben:example.org"]
    tools: [shell, shared_directories]
```

Ann asks `ann_assistant`: "Share your notes with ben_assistant in a shared directory called notes."
Ann cannot use `ben_assistant`, so the invitation is pending, and `ben_assistant` sees only the directory's owner and name, never its path or files; `list_shared_directories()` also shows who asked and when.
When Ben next talks to `ben_assistant`, its prompt mentions the invitation; Ben says "Accept the notes directory from ann_assistant", and it calls `accept_shared_directory("ann_assistant", "notes")` and reads the files from then on.
The same works from `ben_assistant` to `ann_assistant`, and between `team_helper` and either person's agent: `team_helper` invites at once when the person asking can use the invited agent, and otherwise that agent's person accepts.

### Rules

Only the agent that created a directory can invite, remove, or delete, and deleting a directory removes every file in it.
Names use 1 to 64 lowercase letters, digits, `-`, or `_`, starting with a letter or digit.
Private agents can neither create nor join shared directories.
Creating or deleting a directory, and adding or removing a member, restarts each affected agent's dedicated worker on its next tool call, which ends that worker's background shells, tmux sessions, and computer sessions.

An invitation applies at once when the user who asks for it may use the invited agent outside the current room.
Access through `access.users`, administrator rights, `access.members_of_rooms`, or the default grant to members of the agent's managed `rooms` counts, but `access.current_room_members` does not; see [Responder access](../authorization.md#responder-access).
Otherwise the invitation is pending until a user who may use the invited agent, by the same rule, accepts it through that agent with `accept_shared_directory(owner, name)` or declines it with `decline_shared_directory(owner, name)`.
A pending agent sees no path or file of the directory and its worker mounts nothing until the accept; inviting it again while pending changes nothing.
An agent holds at most 10 pending invitations; a further invitation fails until it accepts or declines one, or an owner cancels one.
The owner's `remove_from_shared_directory` and `delete_shared_directory` also cancel a pending invitation.

Under `file_access: workspace`, `filesystem` accepts paths inside the agent's shared directories when it runs in a worker or when MindRoom runs without workers.
When MindRoom uses workers but an agent's `filesystem` runs in the primary process, `workspace` keeps it out of shared directories; add it to the agent's `worker_tools`.
Other path tools, such as `attachments` and `matrix_message`, stay inside the workspace, so copy a shared file into the workspace before sending it.

### Access by deployment

Where tools run decides who reaches a shared directory:

| Where tools run | Owner | Invited agents | Other agents |
|---|---|---|---|
| Dedicated Docker or Kubernetes workers | Read and write | Read only | No access |
| Kubernetes `static_runner` sidecar | Read and write | Read and write | Read and write |
| No workers, through `filesystem` under `file_access: workspace` | Read and write | Read only | No access |

`shell` and tools under `file_access: unrestricted` that run in the primary process read and write every shared directory, whoever owns it, even when other tools use workers.
The documented Compose and host-container `static_runner` setups mount only scratch storage, so tools running there cannot reach shared directories; use dedicated Docker workers instead.
A `worker_scope: user` worker serves several agents, so each of them reaches every directory any of them owns or joined, with the widest access among them.

## [`docker`]

`docker` splits read-only introspection from mutations across three functions: `docker_inspect(action, ...)` (`list_containers`, `inspect_container`, `get_container_logs`, `list_images`, `inspect_image`, `list_volumes`, `inspect_volume`, `list_networks`, `inspect_network`), `docker_container(action, ...)` (`run`, `exec`, `start`, `stop`, `remove`), and `docker_resource(action, ...)` (`pull_image`, `build_image`, `tag_image`, `remove_image`, `create_volume`, `remove_volume`, `create_network`, `remove_network`, `connect_network`, `disconnect_network`).
Docker daemon access is privileged host control, so `docker` runs in a worker by default, and the runtime that runs it needs a reachable Docker daemon or socket.
The client is isolated from host `~/.docker/config.json` / `DOCKER_CONFIG` registry credentials and proxies, and `include_tools` accepts the consolidated names `docker_inspect`, `docker_container`, and `docker_resource`.

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
docker_inspect("list_containers")
docker_container("run", image="postgres:16", name="postgres", detach=True, environment={"POSTGRES_PASSWORD": "example"})
docker_inspect("get_container_logs", target="postgres", tail=50)
```

## [`calculator`]

`calculator` provides `add()`, `subtract()`, `multiply()`, `divide()`, `exponentiate()`, `factorial()`, `is_prime()`, and `square_root()` for exact arithmetic without running arbitrary code.
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
The agent's prompt includes the default reasoning instructions, which tell the model to call `think()` before every tool call or reply, only while both `think()` and `analyze()` are enabled; instructions you set with `instructions` are always included.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_think` | `boolean` | `no` | `true` | Enable `think()`. |
| `enable_analyze` | `boolean` | `no` | `true` | Enable `analyze()`. |
| `add_few_shot` | `boolean` | `no` | `false` | Add few-shot examples to the default reasoning instructions. |
| `instructions` | `text` | `no` | `null` | Replace the default reasoning instructions entirely. |
| `few_shot_examples` | `text` | `no` | `null` | Replace the built-in few-shot examples; used only when few-shot examples are added. |
| `all` | `boolean` | `no` | `false` | Enable all reasoning functions. |

### Example

```yaml
agents:
  researcher:
    tools:
      - reasoning:
          add_few_shot: true
```

## [`file_generation`]

`file_generation` provides a unified `generate_file(format, content, filename=None, title=None, headers=None, language=None)` tool supporting `json`, `csv`, `pdf`, `docx`, `html`, `txt`, and `code`.
Each call returns the generated file in its tool result.
Filenames are generated when omitted, and a missing extension is added for the export type.
`format="json"` accepts dicts, lists, or strings, and wraps a string that is not valid JSON.
PDF and DOCX generation require `reportlab` or `python-docx`.

Files are also saved to disk when `output_directory` is set or `save_files` is `true`.
Saved files go into `output_directory` inside the agent workspace, or the workspace root when only `save_files` is set, and overwrite files of the same name.
`output_directory` must be a relative path that stays inside the workspace.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `output_directory` | `text` | `no` | `null` | Directory inside the agent workspace where generated files are saved. |
| `save_files` | `boolean` | `no` | `false` | Save generated files; when `output_directory` is unset, they go to the workspace root. |
| `enable_json_generation` | `boolean` | `no` | `true` | Enable `format="json"` in `generate_file()`. |
| `enable_csv_generation` | `boolean` | `no` | `true` | Enable `format="csv"` in `generate_file()`. |
| `enable_pdf_generation` | `boolean` | `no` | `true` | Enable `format="pdf"` in `generate_file()`. |
| `enable_docx_generation` | `boolean` | `no` | `true` | Enable `format="docx"` in `generate_file()`. |
| `enable_txt_generation` | `boolean` | `no` | `true` | Enable `format="txt"` in `generate_file()`. |
| `enable_html_generation` | `boolean` | `no` | `true` | Enable `format="html"` in `generate_file()`. |
| `enable_code_generation` | `boolean` | `no` | `true` | Enable `format="code"` in `generate_file()`. |
| `all` | `boolean` | `no` | `false` | Enable all formats in `generate_file()`. |

### Example

```yaml
agents:
  reporter:
    tools:
      - file_generation:
          output_directory: exports
```

```python
generate_file("csv", [{"name": "alpha", "value": 1}, {"name": "beta", "value": 2}], filename="data.csv")
generate_file("pdf", "Quarterly summary", filename="report.pdf", title="Q1 Report")
generate_file("docx", "Quarterly summary", filename="report.docx", title="Q1 Report")
```

## [`visualization`]

`visualization` provides a unified `create_chart(chart_type, ...)` tool supporting `bar`, `line`, `pie`, `scatter`, and `histogram`.
Bar, line, and pie charts accept a dict, a list of dicts, or a JSON string; scatter plots take `x_data`/`y_data` (or `data`), and histograms take a list of numbers.
Each call saves a PNG in `output_dir` inside the agent workspace and returns the file path.
A chart's `filename` must be a plain file name; when omitted, the chart is named after its type and numbered, such as `bar_chart_3.png`.
Charts overwrite files of the same name, and `output_dir` is created when missing.
Because charts are in the workspace, `attachments` and `matrix_message` can send them under the default `file_access: workspace`.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `output_dir` | `text` | `no` | `"charts"` | Directory inside the agent workspace where charts are saved; absolute paths and `..` are rejected. |
| `enable_create_bar_chart` | `boolean` | `no` | `true` | Enable `chart_type="bar"` in `create_chart()`. |
| `enable_create_line_chart` | `boolean` | `no` | `true` | Enable `chart_type="line"` in `create_chart()`. |
| `enable_create_pie_chart` | `boolean` | `no` | `true` | Enable `chart_type="pie"` in `create_chart()`. |
| `enable_create_scatter_plot` | `boolean` | `no` | `true` | Enable `chart_type="scatter"` in `create_chart()`. |
| `enable_create_histogram` | `boolean` | `no` | `true` | Enable `chart_type="histogram"` in `create_chart()`. |
| `all` | `boolean` | `no` | `false` | Enable all chart types in `create_chart()`. |

### Example

```python
create_chart("bar", data={"Mon": 12, "Tue": 18, "Wed": 9}, title="Requests per day")
create_chart("scatter", x_data=[1, 2, 3], y_data=[2, 5, 7], title="Experiment results")
create_chart("histogram", data=[1, 1, 2, 3, 5, 8, 13], title="Value distribution")
```

## [`sleep`]

`sleep()` waits for 0 to 300 seconds and returns a confirmation; other durations return an error without waiting.
The response stays open until the delay ends or the response is stopped.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `enable_sleep` | `boolean` | `no` | `true` | Enable `sleep()`. |
| `all` | `boolean` | `no` | `false` | Enable all sleep functions. |

## Related Docs

- [Tools Overview](index.md)
- [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration)
- [Sandbox Proxy Isolation](../deployment/sandbox-proxy.md)
