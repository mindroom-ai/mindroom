---
icon: lucide/repeat
---

# Automation & Platforms

Use these tools to invoke AWS Lambda functions, send email through Amazon SES, edit Airflow DAG files, run code in hosted E2B or Daytona sandboxes, call Composio-connected apps, or make HTTP requests to APIs that have no dedicated MindRoom tool.

## Tools On This Page

| Tool | Needs | Best for |
| --- | --- | --- |
| [`aws_lambda`] | AWS credentials | Listing and invoking Lambda functions. |
| [`aws_ses`] | AWS credentials and a verified SES sender | Sending plain-text email. |
| [`airflow`] | Nothing | Reading and writing Airflow DAG source files. |
| [`e2b`] | E2B API key | A hosted code interpreter with file transfer, commands, and temporary public URLs. |
| [`daytona`] | Daytona API key | A remote sandbox that keeps state and a working directory across calls. |
| [`composio`] | Composio API key and connected accounts | Calling selected actions from many apps through one Composio account. |
| [`custom_api`] | Nothing, or the target API's credentials | HTTP requests to public APIs. |

## Setup

Add the tool to an agent's `tools` list; see [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration) for inline options and `include_tools`/`exclude_tools`.
`password` fields, including `custom_api` `headers` and Daytona `sandbox_env_vars`, cannot be set inline; see [Security Restrictions](index.md#security-restrictions).
`aws_lambda` and `aws_ses` have no key field and authenticate through the standard AWS credential chain: AWS environment variables, shared config and credentials files, or an instance or pod role.
The sandbox and platform tools read their key from the environment when none is stored:

| Tool | Environment variable |
| --- | --- |
| `e2b` | `E2B_API_KEY` |
| `daytona` | `DAYTONA_API_KEY` (and `DAYTONA_API_URL` for `api_url`) |
| `composio` | `COMPOSIO_API_KEY`, or cached Composio user data |

All these tools run in the primary runtime by default; `e2b`, `daytona`, and `composio` always do, even when listed in `worker_tools` (see [Worker Routing](../deployment/sandbox-proxy.md#worker-routed-execution)).
Missing Python dependencies install on first use; see [Automatic Dependency Installation](index.md#automatic-dependency-installation).
Toolkits with an `all` option enable every function when `all: true`, regardless of the individual `enable_*` flags.

## [`aws_lambda`]

`aws_lambda` provides `list_functions()` and `invoke_function(function_name, payload="{}")` in one AWS region.
`invoke_function()` sends `payload` as a JSON string and returns the Lambda status code and the decoded response payload.
`list_functions()` returns only the first page of results that AWS sends, so it is meant for simple invocation workflows rather than administering large accounts.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `region_name` | `text` | `us-east-1` | AWS region. |
| `enable_list_functions` | `boolean` | `true` | Enable `list_functions()`. |
| `enable_invoke_function` | `boolean` | `true` | Enable `invoke_function()`. |
| `all` | `boolean` | `false` | Enable both functions. |

```yaml
agents:
  automation:
    tools:
      - aws_lambda:
          region_name: us-west-2
```

## [`aws_ses`]

`aws_ses` provides `send_email(subject, body, receiver_email)`, which sends a plain-text email from `sender_name <sender_email>` to one recipient.
Empty subjects and bodies are refused.
It does not support HTML, templates, or attachments.
Verify the sender identity in SES before use, and set both `sender_email` and `sender_name`, because an unset name appears literally as `None` in the sender.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `sender_email` | `text` | `null` | Sender address; required in practice. |
| `sender_name` | `text` | `null` | Sender display name. |
| `region_name` | `text` | `us-east-1` | AWS region of the SES identity. |
| `enable_send_email` | `boolean` | `true` | Enable `send_email()`. |
| `all` | `boolean` | `false` | Enable `send_email()`. |

```yaml
agents:
  notifications:
    tools:
      - aws_ses:
          sender_email: alerts@example.com
          sender_name: MindRoom Alerts
          region_name: us-east-1
```

## [`airflow`]

`airflow` provides `read_dag_file(dag_file)` and `save_dag_file(contents, dag_file)` for editing DAG source files.
It does not talk to the Airflow scheduler or REST API, so it cannot trigger runs or inspect task state.
`dag_file` paths are relative to `dags_dir`, which defaults to the agent workspace and resolves relative to it.
`save_dag_file()` creates missing parent directories, and `read_dag_file()` refuses files larger than 64 MiB.
DAG paths follow the agent's [`file_access`](../architecture/security-posture.md#file-access): with the default `workspace`, files outside the agent workspace are refused, so a DAG folder elsewhere needs `file_access: unrestricted`.
Point `dags_dir` at the folder your Airflow deployment actually watches.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `dags_dir` | `text` | `null` | DAG directory; relative values resolve from the agent workspace. |
| `enable_save_dag_file` | `boolean` | `true` | Enable `save_dag_file()`. |
| `enable_read_dag_file` | `boolean` | `true` | Enable `read_dag_file()`. |
| `all` | `boolean` | `false` | Enable both functions. |

```yaml
agents:
  airflow_editor:
    tools:
      - airflow:
          dags_dir: dags
```

## [`e2b`]

`e2b` runs code in a hosted E2B sandbox.
The tool creates one sandbox when it loads and reuses it for its calls.
It fails to load when sandbox creation fails or no key is available, which reports `E2B_API_KEY not set. Please set the E2B_API_KEY environment variable.`
It provides:

- Code and commands: `run_python_code()`, `run_command()`, `stream_command()`, `run_background_command()`, and `kill_background_command()`.
- Results of the most recent `run_python_code()` call: `download_png_result()` and `download_chart_data()`.
- Sandbox files: `upload_file()`, `download_file_from_sandbox()`, `list_files()`, `read_file_content()`, `write_file_content()`, and `watch_directory()`.
- Servers and lifecycle: `run_server()`, `get_public_url()`, `set_sandbox_timeout()`, `get_sandbox_status()`, `shutdown_sandbox()`, and `list_running_sandboxes()`.

`upload_file()` follows the agent's [`file_access`](../architecture/security-posture.md#file-access) and refuses local files larger than 64 MiB, so fetch larger data from inside the sandbox instead.
Downloads always land inside the agent workspace at workspace-relative paths; absolute paths, `..`, and links leaving the workspace are refused, and agents without a workspace cannot download.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | E2B API key. |
| `timeout` | `number` | `300` | Sandbox timeout in seconds. |
| `sandbox_options` | `text` | `null` | Extra E2B sandbox-creation options; the SDK expects a mapping that tool config cannot express, so leave it unset. |

```yaml
agents:
  remote_exec:
    tools:
      - e2b:
          timeout: 600
```

## [`daytona`]

`daytona` runs code and shell commands in a remote Daytona sandbox.
It provides `run_code()`, `run_shell_command()`, `create_file()`, `read_file()`, `list_files()`, `delete_file()`, and `change_directory()`.
With `persistent: true`, the same sandbox is reused across calls in one agent session; set `sandbox_id` to use a known sandbox across sessions.
The working directory persists in the session, so later relative paths resolve from it.
Change it with `change_directory()` or a `run_shell_command()` call that is only `cd <directory>`; a combined command such as `cd project && ls` fails instead.
When no reusable sandbox is found, the tool creates one, even with `auto_create_sandbox: false`.

`verify_ssl: false` disables certificate checks for every Daytona tool in the MindRoom process until it restarts, exposing the API key and sandbox traffic to anyone who can intercept the connection.
Set it only for a self-hosted Daytona API whose certificate you cannot otherwise trust.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Daytona API key. |
| `api_url` | `url` | `null` | Daytona API URL. |
| `sandbox_id` | `text` | `null` | Existing sandbox to use instead of a session sandbox. |
| `sandbox_language` | `text` | `python` | `python`, `javascript`, or `typescript`. |
| `sandbox_target` | `text` | `null` | Daytona target. |
| `sandbox_os` | `text` | `null` | Accepted but not applied to created sandboxes. |
| `auto_stop_interval` | `number` | `60` | Minutes of inactivity before a created sandbox stops; `0` disables auto-stop. |
| `sandbox_os_user` | `text` | `null` | OS user in the sandbox. |
| `sandbox_env_vars` | `password` | `null` | JSON object of string environment-variable names and values. |
| `sandbox_labels` | `text` | `{}` | JSON object of string label names and values. |
| `organization_id` | `text` | `null` | Daytona organization ID. |
| `timeout` | `number` | `300` | Timeout in seconds for sandbox operations. |
| `auto_create_sandbox` | `boolean` | `true` | Create a replacement sandbox when finding, creating, or starting one fails. |
| `verify_ssl` | `boolean` | `true` | Verify Daytona TLS certificates; see the warning above. |
| `persistent` | `boolean` | `true` | Reuse one sandbox across calls in the current agent session. |
| `sandbox_public` | `boolean` | `null` | Make created sandboxes public. |
| `instructions` | `text` | `null` | Custom toolkit instructions replacing the bundled write, run, and show-results guidance. |
| `add_instructions` | `boolean` | `false` | Add the toolkit instructions to the agent prompt. |

```yaml
agents:
  remote_dev:
    tools:
      - daytona:
          api_url: https://api.daytona.io
          auto_stop_interval: 30
          add_instructions: true
```

## [`composio`]

`composio` exposes the Composio actions listed in `actions` as agent tools.
Callable names are the lowercase action IDs; for example, `GITHUB_GET_THE_AUTHENTICATED_USER` becomes `github_get_the_authenticated_user`.
Before loading the tool, configure the API key and connect the apps' accounts for the chosen `entity_id` in Composio, because the agent cannot create connections itself.
A missing or empty `actions` list fails with `Composio requires a nonempty actions list.`
`composio` does not accept `include_tools` or `exclude_tools`; choose its functions with `actions`.
It is not confined by `file_access` and cannot run in a worker, so enable it only for agents trusted with the primary runtime (see [File access](../architecture/security-posture.md#file-access)).

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `actions` | `string[]` | required | Nonempty list of Composio action IDs, such as `GITHUB_GET_THE_AUTHENTICATED_USER`. |
| `api_key` | `password` | `null` | Composio API key. |
| `base_url` | `url` | `null` | Composio API base URL. |
| `entity_id` | `text` | `default` | Composio entity whose connected accounts run the actions. |
| `workspace_id` | `text` | `null` | Composio workspace ID. |
| `workspace_config` | `text` | `null` | Composio workspace configuration object; tool config cannot express it, so leave it unset. |
| `connected_account_ids` | `text` | `null` | Mapping of apps to connected account IDs; tool config cannot express it, so leave it unset. |
| `metadata` | `text` | `null` | Action metadata mapping; tool config cannot express it, so leave it unset. |
| `processors` | `text` | `null` | Request, response, and schema processor mapping; tool config cannot express it, so leave it unset. |
| `output_dir` | `text` | `null` | Directory for file-based results. |
| `output_in_file` | `boolean` | `false` | Write action output to files. |
| `lockfile` | `text` | `null` | Lockfile path for action-version locking. |
| `lock` | `boolean` | `true` | Enable lockfile-based coordination. |
| `max_retries` | `number` | `3` | Retries for failed Composio operations. |
| `verbosity_level` | `number` | `null` | Verbosity from `0` to `3`. |
| `allow_tracing` | `boolean` | `false` | Enable Composio tracing. |
| `logging_level` | `text` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

```yaml
agents:
  integrations:
    tools:
      - composio:
          actions:
            - GITHUB_GET_THE_AUTHENTICATED_USER
```

## [`custom_api`]

`custom_api` provides `make_request(endpoint, method="GET", params=None, data=None, headers=None, json_data=None)` for public HTTP(S) APIs that have no dedicated MindRoom tool.
`method` accepts `GET`, `POST`, `PUT`, `DELETE`, or `PATCH`.
With `base_url` set, `endpoint` is a path joined to it; without `base_url`, `endpoint` must be a full URL.
Localhost, loopback, private-network, cloud metadata, and non-HTTP(S) destinations are refused, including through redirects, and there is no setting to allow them.

Authentication:

- `api_key` sends `Authorization: Bearer <api_key>`.
- A nonempty `username` and `password` pair sends HTTP Basic Auth instead of the bearer token.
- `headers` adds default headers, and per-call headers are merged over them.

Configured credentials (`api_key`, `username`/`password`, or `headers`) are sent only to the `base_url` origin, also across redirects, except a same-host upgrade from `http` to `https`.
A tool with credentials but no `base_url` sends nothing and returns `custom_api sends its configured api_key, username and password, and headers only to base_url; set base_url to call this API with them, or remove them to call arbitrary URLs`.

The result is JSON with `status_code`, response `headers`, and `data`, which holds the parsed JSON body or `{"text": ...}` for other bodies.
Non-2xx responses add `"error": "Request failed"`.
At most 10 redirects are followed.
A body larger than 8 MiB, or one the server compressed despite being asked not to, returns `status_code`, `headers`, and an `error` instead of `data`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `base_url` | `url` | `null` | Base URL for `endpoint`; required to use `api_key`, `username`/`password`, or `headers`. |
| `username` | `text` | `null` | HTTP Basic Auth username. |
| `password` | `password` | `null` | HTTP Basic Auth password. |
| `api_key` | `password` | `null` | Bearer token. |
| `headers` | `password` | `null` | Default headers as a JSON object of strings, such as `{"X-Api-Key": "value"}`. |
| `verify_ssl` | `boolean` | `true` | Verify TLS certificates. |
| `timeout` | `number` | `30` | Request timeout in seconds. |
| `enable_make_request` | `boolean` | `true` | Enable `make_request()`. |
| `all` | `boolean` | `false` | Enable `make_request()`. |

```yaml
agents:
  api_bridge:
    tools:
      - custom_api:
          base_url: https://api.example.com/v1
          timeout: 20
```

## Related Docs

- [Tools Overview](index.md)
- [Execution & Coding](execution-and-coding.md)
- [Project Management](project-management.md)
- [MCP](../mcp.md)
