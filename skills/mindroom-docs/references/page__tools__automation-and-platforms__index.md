# Automation & Platforms

Use these tools to manage AWS-backed automation, edit Airflow DAG files, run code in hosted sandboxes, call arbitrary HTTP APIs, and bridge into broader integration platforms.

## What This Page Covers

This page documents the built-in tools in the `automation-and-platforms` group.
Use these tools when you need infrastructure automation, generic API access, remote execution environments, or platform-level integration brokers.

## Tools On This Page

- \[`aws_lambda`\] - AWS Lambda function listing and invocation.
- \[`aws_ses`\] - Amazon SES outbound email sending.
- \[`airflow`\] - Local Airflow DAG file reads and writes.
- \[`e2b`\] - Hosted E2B code-execution sandbox with file, command, and server helpers.
- \[`daytona`\] - Persistent remote sandbox and dev-environment execution.
- \[`composio`\] - Dynamic Composio-backed integration toolset for connected external apps.
- \[`custom_api`\] - Generic HTTP requests to arbitrary APIs.

## Common Setup Notes

All seven tools on this page default to the primary agent runtime instead of MindRoom's worker-routed execution set.
`aws_lambda`, `airflow`, and `custom_api` are registered as `setup_type: none`, while `aws_ses`, `e2b`, `daytona`, and `composio` are marked `requires_config`.
`src/mindroom/api/integrations.py` currently only exposes Spotify OAuth routes on this branch, so none of the tools on this page have a dedicated MindRoom OAuth flow.
Password fields such as `api_key` and `password` must be stored through the dashboard or credential store instead of inline YAML.
`aws_lambda` and `aws_ses` both rely on standard boto3 credential resolution, so normal AWS environment variables, shared config files, or instance-role credentials are the real authentication path.
That matters especially for `aws_ses`, because the current registry marks it as `setup_type: api_key` even though the tool itself does not expose an API-key field.
`e2b` accepts `api_key` inline from stored credentials or falls back to `E2B_API_KEY`.
`daytona` accepts stored credentials or environment fallback through `DAYTONA_API_KEY`, and `api_url` can also fall back to `DAYTONA_API_URL`.
`composio` can fall back to cached Composio user data or `COMPOSIO_API_KEY` when `api_key` is not stored directly.
Several fields on this page are advanced raw constructor inputs rather than friendly hand-authored YAML values, including `sandbox_options`, `sandbox_env_vars`, `sandbox_labels`, `workspace_config`, `connected_account_ids`, `metadata`, `processors`, and `headers`.
Missing optional dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.

## \[`aws_lambda`\]

`aws_lambda` lists Lambda functions and invokes them in the configured AWS region.

### What It Does

`aws_lambda` exposes `list_functions()` and `invoke_function(function_name, payload="{}")`.
The toolkit constructs a boto3 Lambda client at init time with `region_name`.
`invoke_function()` passes the payload through to boto3 as a string and returns the Lambda status code plus the decoded response payload.
`list_functions()` currently makes one direct `list_functions()` call rather than using a paginator, so very large accounts may need a richer AWS-specific path than this thin wrapper.

### Configuration

| Option                   | Type      | Required | Default     | Notes                                                      |
| ------------------------ | --------- | -------- | ----------- | ---------------------------------------------------------- |
| `region_name`            | `text`    | `no`     | `us-east-1` | AWS region used when constructing the boto3 Lambda client. |
| `enable_list_functions`  | `boolean` | `no`     | `true`      | Enable `list_functions()`.                                 |
| `enable_invoke_function` | `boolean` | `no`     | `true`      | Enable `invoke_function()`.                                |
| `all`                    | `boolean` | `no`     | `false`     | Enable the full upstream Lambda toolkit surface.           |

### Example

```yaml
agents:
  automation:
    tools:
      - aws_lambda:
          region_name: us-west-2
```

```python
list_functions()
invoke_function("daily-report", payload='{"date": "2026-03-31"}')
```

### Notes

- Configure AWS credentials through the standard boto3 chain rather than expecting a MindRoom-specific key field.
- The payload is a raw string in the upstream wrapper, so JSON requests should be serialized before invocation.
- Use this tool for simple invocation workflows, not full Lambda administration.

## \[`aws_ses`\]

`aws_ses` sends plain-text outbound email through Amazon SES with a configured sender identity.

### What It Does

`aws_ses` exposes `send_email(subject, body, receiver_email)`.
The toolkit builds a boto3 SES client with `region_name` and then sends a plain-text message from `"{sender_name} <{sender_email}>"`.
It validates that `subject` and `body` are non-empty before sending.
The current wrapper does not add HTML email support, templates, attachments, or richer SES delivery controls on top of the basic send call.

### Configuration

| Option              | Type      | Required | Default     | Notes                                                                                                          |
| ------------------- | --------- | -------- | ----------- | -------------------------------------------------------------------------------------------------------------- |
| `sender_email`      | `text`    | `no`     | `null`      | Sender email address used in the SES `Source` header. This is effectively required in practice.                |
| `sender_name`       | `text`    | `no`     | `null`      | Display name used in the SES `Source` header. Set this together with `sender_email` for a clean sender format. |
| `region_name`       | `text`    | `no`     | `us-east-1` | AWS region used when constructing the boto3 SES client.                                                        |
| `enable_send_email` | `boolean` | `no`     | `true`      | Enable `send_email()`.                                                                                         |
| `all`               | `boolean` | `no`     | `false`     | Enable the full upstream SES toolkit surface.                                                                  |

### Example

```yaml
agents:
  notifications:
    tools:
      - aws_ses:
          sender_email: alerts@example.com
          sender_name: MindRoom Alerts
          region_name: us-east-1
```

```python
send_email(
    subject="Nightly sync complete",
    body="The nightly sync finished successfully.",
    receiver_email="ops@example.com",
)
```

### Notes

- Despite the current `requires_config` and `api_key` registry metadata, authentication still comes from the standard boto3 AWS credential chain.
- Verify the sender identity in SES before relying on this tool for real mail delivery.
- This wrapper only sends plain-text email.

## \[`airflow`\]

`airflow` is a local DAG-file helper for reading and writing Airflow Python files.

### What It Does

`airflow` exposes `save_dag_file(contents, dag_file)` and `read_dag_file(dag_file)`.
If `dags_dir` is a string, the upstream toolkit resolves it relative to the current working directory at tool initialization time.
`save_dag_file()` creates missing parent directories before writing the target DAG file.
This tool manages DAG source files only.
It does not talk to the Airflow scheduler, trigger DAG runs, inspect task state, or call the Airflow REST API.

### Configuration

| Option                 | Type      | Required | Default | Notes                                                                                                    |
| ---------------------- | --------- | -------- | ------- | -------------------------------------------------------------------------------------------------------- |
| `dags_dir`             | `text`    | `no`     | `null`  | Base directory for DAG files, resolved relative to the current working directory when given as a string. |
| `enable_save_dag_file` | `boolean` | `no`     | `true`  | Enable `save_dag_file()`.                                                                                |
| `enable_read_dag_file` | `boolean` | `no`     | `true`  | Enable `read_dag_file()`.                                                                                |
| `all`                  | `boolean` | `no`     | `false` | Enable the full upstream Airflow toolkit surface.                                                        |

### Example

```yaml
agents:
  airflow_editor:
    tools:
      - airflow:
          dags_dir: dags
```

```python
read_dag_file("daily_reporting.py")
save_dag_file("from airflow import DAG\n", "generated/new_job.py")
```

### Notes

- Use `airflow` when the job is editing DAG source files, not when you need live Airflow control-plane access.
- `dags_dir` is not a MindRoom-managed workspace root like `base_dir` on some local execution tools.
- Keep the configured directory aligned with the filesystem path your Airflow deployment actually watches.

## \[`e2b`\]

`e2b` provides a hosted code-execution sandbox with Python execution, file transfer, command execution, and temporary public URLs.

### What It Does

`e2b` requires an API key from stored credentials or `E2B_API_KEY`.
The toolkit creates one E2B sandbox at initialization time and reuses it for subsequent tool calls from that toolkit instance.
It exposes `run_python_code()`, `upload_file()`, `download_png_result()`, `download_chart_data()`, `download_file_from_sandbox()`, `run_command()`, `stream_command()`, `run_background_command()`, `kill_background_command()`, `list_files()`, `read_file_content()`, `write_file_content()`, `watch_directory()`, `get_public_url()`, `run_server()`, `set_sandbox_timeout()`, `get_sandbox_status()`, `shutdown_sandbox()`, and `list_running_sandboxes()`.
The media helpers operate on the most recent `run_python_code()` result, which is why chart and PNG download flows are companion actions instead of standalone reads.
`timeout` is passed into `Sandbox.create(...)`, and `sandbox_options` is splatted directly into that constructor.

### Configuration

| Option            | Type       | Required | Default | Notes                                                                                                                                                |
| ----------------- | ---------- | -------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api_key`         | `password` | `no`     | `null`  | E2B API key. The tool also falls back to `E2B_API_KEY`.                                                                                              |
| `timeout`         | `number`   | `no`     | `300`   | Sandbox timeout in seconds passed into `Sandbox.create(...)`.                                                                                        |
| `sandbox_options` | `text`     | `no`     | `null`  | Advanced raw sandbox-constructor options. The upstream constructor expects a dict-like object, while current MindRoom metadata exposes this as text. |

### Example

```yaml
agents:
  remote_exec:
    tools:
      - e2b:
          timeout: 600
```

```python
run_python_code("print('hello from e2b')")
upload_file("data/report.csv", "workspace/report.csv")
run_server("python -m http.server 8000", port=8000)
```

### Notes

- The tool fails fast if no API key is available or if sandbox creation fails during initialization.
- `sandbox_options` is mainly useful for advanced programmatic setup rather than normal handwritten YAML.
- Use `e2b` when you want a cloud code interpreter with file and server helpers, not just a single command runner.

## \[`daytona`\]

`daytona` runs code and shell commands in a remote sandbox that can persist across agent sessions.

### What It Does

`daytona` requires an API key from stored credentials or `DAYTONA_API_KEY`.
`api_url` can also fall back to `DAYTONA_API_URL`.
The toolkit exposes `run_code()`, `run_shell_command()`, `create_file()`, `read_file()`, `list_files()`, `delete_file()`, and `change_directory()`.
When `persistent` is true, the tool stores the active sandbox ID in `agent.session_state` and tries to reuse that sandbox on later calls.
It also tracks a working directory in `agent.session_state`, and `run_shell_command()` treats `cd ...` specially so later relative-path commands and file operations stay in that directory.
If no reusable sandbox exists, the toolkit creates one automatically unless `auto_create_sandbox` is disabled.
The bundled default instructions describe a code-write, execute, and show-results workflow, but those instructions are only added to the agent prompt when `add_instructions: true`.

### Configuration

| Option                | Type       | Required | Default  | Notes                                                                                                                                                                 |
| --------------------- | ---------- | -------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api_key`             | `password` | `no`     | `null`   | Daytona API key. The tool also falls back to `DAYTONA_API_KEY`.                                                                                                       |
| `api_url`             | `url`      | `no`     | `null`   | Daytona API URL. The tool also falls back to `DAYTONA_API_URL`.                                                                                                       |
| `sandbox_id`          | `text`     | `no`     | `null`   | Explicit sandbox ID to reuse instead of creating or looking up a session-backed sandbox.                                                                              |
| `sandbox_language`    | `text`     | `no`     | `PYTHON` | Primary sandbox language, such as `PYTHON`, `JAVASCRIPT`, or `TYPESCRIPT`.                                                                                            |
| `sandbox_target`      | `text`     | `no`     | `null`   | Daytona target passed into `DaytonaConfig`.                                                                                                                           |
| `sandbox_os`          | `text`     | `no`     | `null`   | Declared sandbox OS field. The current creation path stores this value but does not pass it into `CreateSandboxFromSnapshotParams`.                                   |
| `auto_stop_interval`  | `number`   | `no`     | `60`     | Auto-stop interval in minutes for created sandboxes.                                                                                                                  |
| `sandbox_os_user`     | `text`     | `no`     | `null`   | OS user for the sandbox.                                                                                                                                              |
| `sandbox_env_vars`    | `text`     | `no`     | `null`   | Advanced raw environment-variable mapping for the sandbox. The upstream constructor expects a dict-like object, while current MindRoom metadata exposes this as text. |
| `sandbox_labels`      | `text`     | `no`     | `{}`     | Advanced raw label mapping for the sandbox. The upstream constructor expects a dict-like object, while current MindRoom metadata exposes this as text.                |
| `organization_id`     | `text`     | `no`     | `null`   | Daytona organization ID.                                                                                                                                              |
| `timeout`             | `number`   | `no`     | `300`    | Timeout in seconds for sandbox operations.                                                                                                                            |
| `auto_create_sandbox` | `boolean`  | `no`     | `true`   | Create a new sandbox automatically when lookup or reuse fails.                                                                                                        |
| `verify_ssl`          | `boolean`  | `no`     | `false`  | Verify Daytona SSL certificates. The default `false` path monkey-patches the Daytona client to disable SSL verification warnings and checks.                          |
| `persistent`          | `boolean`  | `no`     | `true`   | Reuse the same sandbox across agent-session calls instead of creating a fresh sandbox each time.                                                                      |
| `sandbox_public`      | `boolean`  | `no`     | `null`   | Whether created sandboxes should be public.                                                                                                                           |
| `instructions`        | `text`     | `no`     | `null`   | Custom toolkit instructions that replace the bundled default instructions.                                                                                            |
| `add_instructions`    | `boolean`  | `no`     | `false`  | Add the toolkit instructions into the agent prompt.                                                                                                                   |

### Example

```yaml
agents:
  remote_dev:
    tools:
      - daytona:
          api_url: https://api.daytona.io
          sandbox_language: PYTHON
          auto_stop_interval: 30
          persistent: true
          add_instructions: true
```

```python
run_code("print('hello from daytona')")
run_shell_command("pwd && ls -la")
change_directory("project")
create_file("main.py", "print('ok')")
```

### Notes

- `sandbox_env_vars` and `sandbox_labels` are advanced constructor inputs rather than convenient hand-authored YAML fields.
- `verify_ssl: false` is not a cosmetic flag here, because the current implementation actively patches the Daytona client to skip certificate verification.
- Use `sandbox_id` when you want to pin the tool to a known sandbox instead of letting session-state reuse choose one.

## \[`composio`\]

`composio` is a dynamic bridge into Composio's connected-app ecosystem rather than a fixed list of built-in actions.

### What It Does

The registered MindRoom tool instantiates `composio_agno.ComposioToolSet`.
That upstream object does not expose a stable fixed method list like `aws_lambda` or `custom_api`.
Instead, its main surface is `get_tools(actions=..., apps=..., tags=...)`, which wraps selected Composio actions into Agno `Toolkit` objects at runtime.
The resulting callable tools therefore depend on your Composio workspace, connected accounts, and action selection rather than a static MindRoom-defined function list.
MindRoom's current registry metadata on this branch documents the connection and workspace fields, but it does not expose separate per-agent app or action filter fields in `config.yaml`.

### Configuration

| Option                  | Type       | Required | Default   | Notes                                                                                                                                                                  |
| ----------------------- | ---------- | -------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api_key`               | `password` | `no`     | `null`    | Composio API key. The SDK can also fall back to cached Composio user data or `COMPOSIO_API_KEY`.                                                                       |
| `base_url`              | `url`      | `no`     | `null`    | Optional Composio API base URL.                                                                                                                                        |
| `entity_id`             | `text`     | `no`     | `default` | Composio entity identifier used when executing actions.                                                                                                                |
| `workspace_id`          | `text`     | `no`     | `null`    | Optional Composio workspace identifier.                                                                                                                                |
| `workspace_config`      | `text`     | `no`     | `null`    | Advanced raw workspace configuration. The upstream constructor expects a workspace-config object, while current MindRoom metadata exposes this as text.                |
| `connected_account_ids` | `text`     | `no`     | `null`    | Advanced raw mapping of app names to connected account IDs. The upstream constructor expects a dict-like object, while current MindRoom metadata exposes this as text. |
| `metadata`              | `text`     | `no`     | `null`    | Advanced raw metadata mapping used by Composio actions and processors.                                                                                                 |
| `processors`            | `text`     | `no`     | `null`    | Advanced raw processor mapping for request, response, or schema hooks.                                                                                                 |
| `output_dir`            | `text`     | `no`     | `null`    | Optional output directory for file-based results.                                                                                                                      |
| `lockfile`              | `text`     | `no`     | `null`    | Optional lockfile path for action-version locking and concurrency control.                                                                                             |
| `max_retries`           | `number`   | `no`     | `3`       | Maximum retries for failed Composio operations.                                                                                                                        |
| `verbosity_level`       | `number`   | `no`     | `null`    | Optional verbosity level.                                                                                                                                              |
| `output_in_file`        | `boolean`  | `no`     | `false`   | Write operation output to files instead of only returning it directly.                                                                                                 |
| `allow_tracing`         | `boolean`  | `no`     | `false`   | Enable tracing support for debugging.                                                                                                                                  |
| `lock`                  | `boolean`  | `no`     | `true`    | Enable lockfile-based coordination.                                                                                                                                    |
| `logging_level`         | `text`     | `no`     | `INFO`    | Composio logging level such as `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.                                                                                     |

### Example

```yaml
agents:
  integrations:
    tools:
      - composio:
          entity_id: default
          workspace_id: workspace_123
          logging_level: INFO
```

### Notes

- The resulting callable actions are dynamic and depend on the Composio workspace rather than on fixed MindRoom-defined function names.
- `workspace_config`, `connected_account_ids`, `metadata`, and `processors` are advanced constructor inputs and are not the most ergonomic handwritten YAML fields in the current metadata model.
- Use `composio` when you want one external platform to broker many app integrations instead of configuring each app-specific tool directly in MindRoom.

## \[`custom_api`\]

`custom_api` is the generic escape hatch for making HTTP requests to APIs that do not have a dedicated MindRoom tool.

### What It Does

`custom_api` exposes `make_request(endpoint, method="GET", params=None, data=None, headers=None, json_data=None)`.
If `base_url` is set, the tool joins it with the passed endpoint.
If `username` and `password` are configured, the request uses HTTP Basic Auth.
If `api_key` is configured, the tool adds `Authorization: Bearer <api_key>` to the default headers.
Per-call headers are merged on top of configured default headers.
The response body is parsed as JSON when possible and otherwise returned as plain text inside a JSON envelope with `status_code`, response `headers`, and `data`.
Non-2xx responses still return a structured result object, with an added `"error": "Request failed"` field.

### Configuration

| Option                | Type       | Required | Default | Notes                                                                                                                                           |
| --------------------- | ---------- | -------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `base_url`            | `url`      | `no`     | `null`  | Base URL joined with the `endpoint` argument when set.                                                                                          |
| `username`            | `text`     | `no`     | `null`  | Optional HTTP Basic Auth username.                                                                                                              |
| `password`            | `password` | `no`     | `null`  | Optional HTTP Basic Auth password stored through the dashboard or credential store.                                                             |
| `api_key`             | `password` | `no`     | `null`  | Optional bearer token stored through the dashboard or credential store.                                                                         |
| `headers`             | `text`     | `no`     | `null`  | Advanced raw default-header mapping. The upstream constructor expects a dict-like object, while current MindRoom metadata exposes this as text. |
| `verify_ssl`          | `boolean`  | `no`     | `true`  | Verify SSL certificates for outgoing HTTPS requests.                                                                                            |
| `timeout`             | `number`   | `no`     | `30`    | Request timeout in seconds.                                                                                                                     |
| `enable_make_request` | `boolean`  | `no`     | `true`  | Enable `make_request()`.                                                                                                                        |
| `all`                 | `boolean`  | `no`     | `false` | Enable the full upstream custom-API toolkit surface.                                                                                            |

### Example

```yaml
agents:
  api_bridge:
    tools:
      - custom_api:
          base_url: https://api.example.com/v1
          verify_ssl: true
          timeout: 20
```

```python
make_request("health")
make_request("users/42", method="GET")
make_request("reports", method="POST", json_data={"range": "7d"})
```

### Notes

- If `base_url` is omitted, `endpoint` must be a full URL.
- If both Basic Auth and `api_key` are configured, the request will send both the `Authorization: Bearer ...` header and the Basic Auth credentials because the wrapper does not treat them as mutually exclusive modes.
- `headers` is an advanced constructor input rather than a polished hand-authored YAML field on this branch.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Execution & Coding](https://docs.mindroom.chat/tools/execution-and-coding/index.md)
- [Project Management](https://docs.mindroom.chat/tools/project-management/index.md)
- [MCP](https://docs.mindroom.chat/mcp/index.md)
