# MCP

MindRoom is a Model Context Protocol (MCP) client: it connects to the MCP servers you configure, discovers their tools, and lets agents call them.
MindRoom uses MCP tools only; it does not use MCP resources or prompts.
To go the other way and expose your agents' tools to external MCP clients, use the [MCP Gateway](https://docs.mindroom.chat/deployment/mcp-gateway/).

## Add an MCP Server

Configure servers in the top-level `mcp_servers` block of `config.yaml`.
Each key is a server ID made of letters, numbers, and underscores.
Each enabled server becomes one MindRoom tool named `mcp_<server_id>`; add that name to an agent's `tools:` list to give the agent the server's tools.

```yaml
mcp_servers:
  chrome_devtools:
    transport: stdio
    command: npx
    args:
      - -y
      - chrome-devtools-mcp@latest
    tool_prefix: chrome

agents:
  browser:
    display_name: Browser
    role: Debug and inspect web apps in Chrome
    model: sonnet
    tools:
      - mcp_chrome_devtools
```

MCP tools work on every worker scope, including private agents.

## Transports

| `transport` | Use for | Required | Optional | Not allowed |
| --- | --- | --- | --- | --- |
| `stdio` | A local server that MindRoom starts as a subprocess | `command` | `args`, `cwd`, `env` | `url`, `headers`, `auth` |
| `sse` | A remote server with a Server-Sent Events endpoint | `url` | `headers`, `auth` | `command`, `args`, `cwd`, `env` |
| `streamable-http` | A remote server with a streamable HTTP endpoint | `url` | `headers`, `auth` | `command`, `args`, `cwd`, `env` |

```yaml
mcp_servers:
  remote_http:
    transport: streamable-http
    url: https://mcp.example.com/mcp
    headers:
      Authorization: Bearer ${MCP_API_TOKEN}
```

Remote transports refuse loopback and private-network addresses; run local servers with `stdio` instead.

### Credentials in Server Config

`env` and `headers` values support `${ENV_VAR}` placeholders, resolved from MindRoom's environment when the connection opens.
Pass `stdio` credentials through `env`, never through `args`.
`config_manager` and `!config show` mask `env` and `headers` values, except values that are a whole `${ENV_VAR}` placeholder, but show `args` as written, and `args` support no placeholders.

Without OAuth, every agent and requester shares one server session and the same static `headers`, and MindRoom never passes the requester's identity or credentials to the server.
For per-user or per-agent accounts on a remote server, use [OAuth](#oauth-backed-remote-mcp).

## Tool Names

There are two names to keep apart:

1. The tool entry in an agent's `tools:` list is `mcp_<server_id>`.
2. The function names the model sees are `<prefix>_<remote_tool_name>`, where the prefix is `tool_prefix` or, if unset, the server ID.

With `tool_prefix: chrome`, a remote tool named `navigate_page` becomes `chrome_navigate_page`.
A final function name must be 64 characters or fewer.
MindRoom rejects duplicate function names within one server and across the tools and servers visible to the same agent; agents that do not share the colliding surfaces may use identical names.

## Choose Which Tools an Agent Sees

`include_tools` and `exclude_tools` on the server filter the catalog for every agent.
To narrow it for one agent, set overrides where you assign the tool:

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

Per-agent overrides accept `include_tools`, `exclude_tools`, and `call_timeout_seconds` (greater than 0).
Filters match remote MCP tool names, not prefixed function names, and a name cannot appear in both `include_tools` and `exclude_tools`.

## Dashboard Name and Icon

`display_name`, `summary`, and `icon` control how the server appears in the dashboard tool catalog and on Connections; they do not change tool names or permissions.

```yaml
mcp_servers:
  team_wiki:
    display_name: Team Wiki
    summary: Search and edit team documentation
    icon: SiConfluence
    transport: streamable-http
    url: https://wiki.example.com/mcp
```

`icon` takes a Lucide icon name such as `Book` or `Calendar`, or a bundled React Icons name such as `SiConfluence` or `SiGooglecalendar`.
Without a usable icon, Connections picks one matching the server's name, or a plug icon.

## Server Options

| Option | Type | Default | Notes |
|--------|------|---------|-------|
| `enabled` | bool | `true` | Set to `false` to disable the server without removing its config |
| `transport` | string | *required* | `stdio`, `sse`, or `streamable-http` |
| `command` | string | `null` | Required for `stdio` |
| `args` | list[string] | `[]` | `stdio` arguments; shown unmasked, so never put credentials here |
| `cwd` | string | `null` | `stdio` working directory |
| `env` | map[string,string] | `{}` | `stdio` environment variables; supports `${ENV_VAR}` |
| `url` | string | `null` | Required for `sse` and `streamable-http` |
| `headers` | map[string,string] | `{}` | Remote request headers; supports `${ENV_VAR}` |
| `auth` | object | `null` | OAuth settings for remote servers; see [OAuth Settings](#oauth-settings) |
| `tool_prefix` | string | server ID | Prefix for model-visible function names; letters, numbers, and underscores |
| `include_tools` | list[string] | `[]` | Allowlist of remote tool names |
| `exclude_tools` | list[string] | `[]` | Denylist of remote tool names; must not overlap `include_tools` |
| `display_name` | string | `null` | Catalog name; falls back to `auth.display_name`, then a name derived from the server ID |
| `summary` | string | `null` | Short capability summary for the dashboard and catalog |
| `icon` | string | `null` | Dashboard and Connections icon name |
| `description` | string | `null` | What the server offers, added to the OAuth bridge tool descriptions the model sees; requires `auth` |
| `required` | bool | `false` | Keep dependent agents and teams from starting while the server is unavailable |
| `startup_timeout_seconds` | float | `20.0` | Time allowed to connect, initialize, and list tools; must be greater than 0 |
| `call_timeout_seconds` | float | `120.0` | Default timeout per tool call; must be greater than 0 |
| `max_concurrent_calls` | int | `1` | Maximum concurrent tool calls to the server; at least 1 |
| `auto_reconnect` | bool | `true` | Reconnect for later calls after a dropped connection or timeout; the failed call is never replayed |

## OAuth-Backed Remote MCP

Use `auth.type: oauth` when a remote MCP server needs an OAuth bearer token, so each connection belongs to the right user or agent.
OAuth requires `sse` or `streamable-http`.

```yaml
mcp_servers:
  example:
    transport: streamable-http
    url: https://mcp.example.com/mcp
    tool_prefix: example
    description: Example workspace search, documents, and calendar for the signed-in user.
    auth:
      type: oauth
      display_name: Example MCP
      discovery: auto

agents:
  assistant:
    display_name: Assistant
    role: Use the user's Example workspace
    model: sonnet
    worker_scope: user_agent
    tools:
      - mcp_example
```

The connection follows the agent's effective scope (`private.per`, then `worker_scope`, then `defaults.worker_scope`), as described in [Where Connections Are Stored](https://docs.mindroom.chat/oauth-framework/#where-connections-are-stored).
Changing an agent's scope changes whose connection it uses, and MindRoom never copies a credential into the new scope, so reconnect there.

### Connecting and Bridge Tools

Every OAuth-backed server always exposes three functions:

- `<prefix>_connection_status`
- `<prefix>_list_tools`
- `<prefix>_call_tool`

Before the account is connected, these are the only functions the model sees, so set `description` to tell the model what connecting unlocks.
When no account is connected, they return a connect link (see [OAuth onboarding in conversation](https://docs.mindroom.chat/oauth-framework/#mindroom-managed-oauth-onboarding-in-conversation)).
After connecting, `<prefix>_list_tools` returns the server's tools and `<prefix>_call_tool` calls them with that connection's token.
Once MindRoom has loaded the catalog for that connection, the agent also gets typed `<prefix>_<remote_tool_name>` functions.

A temporary token refresh failure returns `MCP server '<server_id>' OAuth token refresh failed; retry shortly` and keeps the stored credentials.
To reset a stuck or revoked connection, see [Reset A Connection](https://docs.mindroom.chat/oauth-framework/#reset-a-connection).

### Endpoint Discovery

With `discovery: auto`, MindRoom reads the OAuth protected-resource metadata (`/.well-known/oauth-protected-resource`) for `resource`, or `url` when `resource` is unset, then fetches the advertised authorization server's metadata.
If no authorization server is advertised, it looks for authorization-server metadata at the resource's origin.
Setting `authorization_server` skips the protected-resource lookup, and explicit `authorization_url`, `token_url`, or `registration_url` values override the discovered endpoints.
Discovery fails if the authorization server does not support the configured `token_endpoint_auth_method` or PKCE method.

Use `discovery: manual` when the server publishes no metadata or you want fixed endpoints:

```yaml
mcp_servers:
  example_manual:
    transport: streamable-http
    url: https://mcp.example.com/mcp
    auth:
      type: oauth
      discovery: manual
      authorization_url: https://mcp.example.com/oauth/authorize
      token_url: https://mcp.example.com/oauth/token
      registration_url: https://mcp.example.com/oauth/register
```

Discovery requires HTTPS, refuses loopback and private-network hosts, and does not follow redirects.
For local development, set `MINDROOM_MCP_OAUTH_ALLOW_INSECURE_DISCOVERY=1` to allow non-HTTPS discovery URLs and `MINDROOM_MCP_OAUTH_ALLOW_PRIVATE_DISCOVERY=1` to allow loopback or private-network discovery hosts.
These variables do not relax the address rules for the MCP connection itself.

### OAuth Client Registration

With `dynamic_client_registration: true` and no stored client, MindRoom registers a client when the first user connects and reuses it for later users.
See [Built-In Providers](https://docs.mindroom.chat/oauth-framework/#built-in-providers) for when a dynamically registered client works from a hosted address.
If discovery later resolves a different token endpoint, MindRoom registers a new client there and logs `oauth_dynamic_client_reregistered`, and users connected through the old client must reconnect.
With dynamic registration disabled, or when the new server offers no registration endpoint, connecting fails instead.

To use your own OAuth app, store its client config in the credential service `<provider_id>_oauth_client`, or list other services in `client_config_services`.
Tokens are stored in `<provider_id>_oauth`; when `provider_id` does not start with `mcp_`, both service names get an `mcp_` prefix.
Public clients (`token_endpoint_auth_method: none`) need only `client_id`; confidential clients also need `client_secret`.
MindRoom never re-registers an operator-configured client, so for a confidential client pin `authorization_server` or `token_url` to keep the server's metadata from redirecting its secret to another token endpoint.
See [Provider And Credential Service Rules](https://docs.mindroom.chat/oauth-framework/#provider-and-credential-service-rules) for client config service rules.

### OAuth Settings

| `auth` field | Type | Default | Notes |
| --- | --- | --- | --- |
| `type` | string | *required* | Must be `oauth` |
| `provider_id` | string | `mcp_<server_id>` | OAuth provider ID; letters, numbers, and underscores |
| `display_name` | string | `null` | Provider name; defaults to `MCP <Server Id>` |
| `resource` | string | server `url` | Protected resource used for discovery |
| `discovery` | string | `auto` | `auto` or `manual` |
| `authorization_server` | string | `null` | Authorization server issuer or base URL; skips protected-resource discovery |
| `authorization_url` | string | `null` | Authorization endpoint; required for `manual` |
| `token_url` | string | `null` | Token endpoint; required for `manual` |
| `registration_url` | string | `null` | Dynamic client registration endpoint |
| `dynamic_client_registration` | bool | `true` | Allow registering a client automatically |
| `token_endpoint_auth_method` | string | `none` | `none`, `client_secret_post`, or `client_secret_basic` |
| `pkce_code_challenge_method` | string | `S256` | `S256`, or `null` to disable PKCE |
| `scopes` | list[string] | `[]` | Requested OAuth scopes |
| `extra_auth_params` | map[string,string] | `{}` | Extra authorization request parameters, such as `resource` |
| `extra_token_params` | map[string,string] | `{}` | Extra code-exchange and refresh parameters; treated as secret |
| `client_config_services` | list[string] | `[]` | Credential services holding the OAuth client config, in lookup order; defaults to `<provider_id>_oauth_client` |
| `shared_client_config_services` | list[string] | `[]` | Shared client config services checked after `client_config_services` |

## Examples

### Echo Server

A minimal local server, saved as `echo_mcp_server.py`:

```python
from mcp.server.fastmcp import FastMCP

server = FastMCP("Echo Server")


@server.tool()
def echo(text: str) -> str:
    return f"echo:{text}"


if __name__ == "__main__":
    server.run()
```

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

The model sees the remote `echo` tool as `echo_echo`.
Use a Python interpreter that has the `mcp` package installed.
To serve it remotely instead, run `server.run(transport="sse")` (endpoint `/sse`) or `server.run(transport="streamable-http")` (endpoint `/mcp`).

### Chrome DevTools

The [Add an MCP Server](#add-an-mcp-server) example starts `chrome-devtools-mcp`, which launches its own Chrome with a dedicated profile.
To attach to an already running debuggable Chrome instead, add `--browser-url`:

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

If Chrome starts slowly, increase `startup_timeout_seconds`; if browser operations run long, increase `call_timeout_seconds`.

### MemPalace Memory

[MemPalace](https://github.com/milla-jovovich/mempalace) is a local memory store with MCP tools for search and adding memories.
Running it through `uvx` keeps its ChromaDB version separate from MindRoom's:

```yaml
mcp_servers:
  mempalace:
    transport: stdio
    command: uvx
    args:
      - --from
      - mempalace
      - python
      - -m
      - mempalace.mcp_server
      - --palace
      - /path/to/.mempalace/palace
    startup_timeout_seconds: 30
    call_timeout_seconds: 60

agents:
  assistant:
    display_name: Assistant
    role: General assistant with persistent memory
    model: sonnet
    tools:
      - mcp_mempalace
```

Initialize and seed the same palace before the server can return results:

```bash
uvx mempalace --palace /path/to/.mempalace/palace init /path/to/content
uvx mempalace --palace /path/to/.mempalace/palace mine /path/to/content
```

See the [MemPalace CLI reference](https://mempalaceofficial.com/reference/cli) for these commands.

## Failures and Catalog Changes

MindRoom connects to MCP servers at startup and whenever `config.yaml` changes, and logs a warning for each server that fails to start, initialize, or list valid tools.
By default a failed server does not block anything: agents and teams that use it start without its tools.
MindRoom keeps retrying a failed server without OAuth in the background and restarts the affected agents and teams once it recovers.
An OAuth-backed server is retried on its next tool call.
Set `required: true` to keep dependent agents and teams from starting until the server is available.

A function-name collision is not retried, and the colliding servers' tools stay unavailable.
Fix the remote tool names or `tool_prefix` values, then reload `config.yaml` or restart MindRoom.

Errors that the MCP server reports for a tool call return to the agent as tool errors and are not retried.
If the connection drops or times out during a call, the call fails even when `auto_reconnect` restores the connection, and the action is never replayed.
Check whether a call that changes something actually completed before retrying it.

When a server reports that its tool list changed, MindRoom reloads the list, at most once a minute per server.
If the tools changed, the agents and teams that use the server restart to pick them up, waiting for active responses like a [config reload](https://docs.mindroom.chat/configuration/#configuration-file).
