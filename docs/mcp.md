---
icon: lucide/plug
---

# MCP

Model Context Protocol (MCP) is a standard way for AI applications to connect to external tool servers.
MindRoom's Phase 1 MCP support acts as an MCP client for tools.
It connects to configured servers, discovers their tool catalogs, and exposes those tools to agents.
MindRoom does not yet consume MCP resources or prompts.

## Configuration Overview

Configure MCP servers in the top-level `mcp_servers` block in `config.yaml`.
Each key is the server ID.
Server IDs must use only letters, numbers, and underscores because MindRoom uses them in tool names.

```yaml
mcp_servers:
  chrome_devtools:
    transport: stdio
    command: npx
    args:
      - -y
      - chrome-devtools-mcp@latest
```

Each configured server creates one dynamic MindRoom tool named `mcp_<server_id>`.
Add that tool name to an agent's `tools:` list to let the agent use the server's discovered MCP tools.

## Transport Types

MindRoom supports three MCP transport types:

- `stdio`
- `sse`
- `streamable-http`

### `stdio`

Use `stdio` when MindRoom should start the MCP server as a subprocess.

```yaml
mcp_servers:
  echo:
    transport: stdio
    command: python
    args:
      - ./echo_mcp_server.py
    cwd: .
    env:
      ECHO_PREFIX: "echo:"
```

For `stdio` servers, `command` is required.
`args`, `cwd`, and `env` are optional.
`url` and `headers` are not allowed on this transport.

### `sse`

Use `sse` for MCP servers that expose a Server-Sent Events endpoint.

```yaml
mcp_servers:
  remote_sse:
    transport: sse
    url: http://127.0.0.1:9000/sse
    headers:
      Authorization: Bearer ${MCP_API_TOKEN}
```

For `sse` servers, `url` is required.
`headers` are optional.
`command`, `args`, `cwd`, and `env` are not allowed on this transport.

### `streamable-http`

Use `streamable-http` for MCP servers that expose the newer streamable HTTP endpoint.

```yaml
mcp_servers:
  remote_http:
    transport: streamable-http
    url: http://127.0.0.1:9000/mcp
    headers:
      Authorization: Bearer ${MCP_API_TOKEN}
```

For `streamable-http` servers, `url` is required.
`headers` are optional.
`command`, `args`, `cwd`, and `env` are not allowed on this transport.

`env` and `headers` values support `${ENV_VAR}` interpolation.
MindRoom resolves those placeholders from the current runtime environment when it opens the MCP transport.

## Per-Server Options

| Option | Type | Default | Notes |
|--------|------|---------|-------|
| `enabled` | bool | `true` | Set to `false` to disable one server without removing its config |
| `transport` | string | *required* | One of `stdio`, `sse`, or `streamable-http` |
| `command` | string | `null` | Required for `stdio` |
| `args` | list[string] | `[]` | Optional `stdio` arguments |
| `cwd` | string | `null` | Optional `stdio` working directory |
| `env` | map[string,string] | `{}` | Optional `stdio` environment variables; supports `${ENV_VAR}` placeholders |
| `url` | string | `null` | Required for `sse` and `streamable-http` |
| `headers` | map[string,string] | `{}` | Optional remote transport headers; supports `${ENV_VAR}` placeholders |
| `tool_prefix` | string | server ID | Prefix for model-visible function names |
| `include_tools` | list[string] | `[]` | Optional allowlist of remote tool names to expose |
| `exclude_tools` | list[string] | `[]` | Optional denylist of remote tool names to hide |
| `startup_timeout_seconds` | float | `20.0` | Maximum time to open the transport, initialize, and discover tools |
| `call_timeout_seconds` | float | `120.0` | Default timeout for each tool call |
| `auto_reconnect` | bool | `true` | Retry once after connection or timeout failures during a call |
| `max_concurrent_calls` | int | `1` | Maximum concurrent tool calls for that server |
| `idle_ttl_seconds` | int | `900` | Reserved for future idle cleanup; it does not currently change runtime behavior |

`tool_prefix` must use only letters, numbers, and underscores.
`include_tools` and `exclude_tools` are matched against the remote MCP tool names, not the MindRoom-prefixed function names.
`include_tools` and `exclude_tools` cannot overlap.

## Agent Access

Each MCP server becomes one MindRoom tool named `mcp_<server_id>`.
Add that name to an agent's `tools:` list to expose the server's discovered tools.

```yaml
mcp_servers:
  chrome_devtools:
    transport: stdio
    command: npx
    args:
      - -y
      - chrome-devtools-mcp@latest

agents:
  browser:
    display_name: Browser
    role: Debug and inspect web apps in Chrome
    model: sonnet
    tools:
      - mcp_chrome_devtools
```

You can also apply per-agent overrides when you assign the MCP tool:

```yaml
agents:
  browser:
    tools:
      - mcp_chrome_devtools:
          include_tools:
            - new_page
            - navigate_page
            - take_snapshot
          call_timeout_seconds: 180
```

These per-agent overrides filter the already discovered catalog for that agent assignment.
They are useful when one server exposes many tools but one agent should see only a focused subset.

MCP integrations are treated as shared-only integrations.
Agents using `worker_scope: user` or `worker_scope: user_agent` cannot use `mcp_<server_id>` tools.
Use unscoped execution or `worker_scope: shared` instead.

## Tool Naming

There are two names to keep in mind:

1. The MindRoom tool entry that you put in `tools:` is `mcp_<server_id>`.
2. The model-visible function names inside that toolkit are `<prefix>_<remote_tool_name>`.

If `tool_prefix` is omitted, MindRoom uses the server ID as the prefix.

For example, this config:

```yaml
mcp_servers:
  chrome_devtools:
    transport: stdio
    command: npx
    args:
      - -y
      - chrome-devtools-mcp@latest
    tool_prefix: chrome
```

means a remote MCP tool named `navigate_page` becomes `chrome_navigate_page` inside the agent's tool list.

MindRoom rejects duplicate function names after prefixing.
That includes collisions inside one server and collisions across different servers.
The final function name must also fit within 64 characters.

## Example: Echo MCP Server

This is the smallest useful local example.
Save it as `echo_mcp_server.py`:

```python
from mcp.server.fastmcp import FastMCP

server = FastMCP("Echo Server")


@server.tool()
def echo(text: str) -> str:
    return f"echo:{text}"


if __name__ == "__main__":
    server.run()
```

Then point MindRoom at it:

```yaml
mcp_servers:
  echo:
    transport: stdio
    command: ./.venv/bin/python
    args:
      - ./echo_mcp_server.py

agents:
  code:
    display_name: Code
    role: Test MCP tools
    model: sonnet
    tools:
      - mcp_echo
```

With that setup, the remote `echo` tool is exposed to the model as `echo_echo`.
If you prefer a shorter function name, set `tool_prefix` to something else.
Use whatever Python interpreter has the `mcp` package installed.
Inside this repository, `./.venv/bin/python` is usually the right choice.

The same server can also be run over HTTP transports.
If you change the script to `server.run(transport="sse")`, the default FastMCP endpoint is `/sse`.
If you change it to `server.run(transport="streamable-http")`, the default FastMCP endpoint is `/mcp`.

## Example: Chrome DevTools MCP

Chrome DevTools MCP is a good fit for browser debugging, page inspection, performance work, and interactive web automation.

```yaml
mcp_servers:
  chrome_devtools:
    transport: stdio
    command: npx
    args:
      - -y
      - chrome-devtools-mcp@latest
    tool_prefix: chrome
    startup_timeout_seconds: 20

agents:
  browser:
    display_name: Browser
    role: Debug web apps with Chrome DevTools
    model: sonnet
    tools:
      - mcp_chrome_devtools
```

This exposes Chrome DevTools functions with names like `chrome_<tool_name>`.
By default, `chrome-devtools-mcp` starts its own Chrome instance with a dedicated profile.

To attach to an already running debuggable Chrome instance instead, add `--browser-url=http://127.0.0.1:9222`:

```yaml
mcp_servers:
  chrome_devtools:
    transport: stdio
    command: npx
    args:
      - -y
      - chrome-devtools-mcp@latest
      - --browser-url=http://127.0.0.1:9222
    tool_prefix: chrome
```

If Chrome startup is slow on your machine, increase `startup_timeout_seconds`.
If individual browser operations can take a while, increase `call_timeout_seconds`.

## Error Handling

MindRoom connects to MCP servers during startup and whenever `config.yaml` changes.
If a server fails to start, initialize, or publish a valid tool catalog, MindRoom marks that server as failed and logs a warning.

Agents and teams that reference `mcp_<server_id>` are blocked from starting while that server is failed.
MindRoom retries failed MCP discovery during later syncs and config reloads.

During tool execution, explicit MCP tool failures are surfaced as tool errors.
Those explicit server-side errors are not retried automatically.

Connection drops and timeouts are treated differently.
When `auto_reconnect: true`, MindRoom refreshes the server connection and retries the tool call once.
If reconnect also fails, the error is surfaced to the caller.

If an MCP server sends a `tools/list_changed` notification, MindRoom refreshes that server's catalog.
If the catalog changed, MindRoom restarts the agents and teams that reference that server so they pick up the updated tool list.

## Limitations

- Phase 1 supports MCP tools only.
- MCP resources and prompts are not exposed in MindRoom yet.
- MCP integrations are shared-only and cannot be used with `worker_scope: user` or `worker_scope: user_agent`.
- `server_id` and `tool_prefix` must use letters, numbers, and underscores.
- The final function name `<prefix>_<remote_tool_name>` must be 64 characters or fewer.
- `idle_ttl_seconds` is reserved for future cleanup and does not currently change runtime behavior.
