---
icon: lucide/monitor
---

# Worker Computer

Worker Computer gives an agent a visible, persistent Chromium browser inside its dedicated worker, and lets the user watch it, take control, and hand it back from MindRoom Chat's Computer panel.
Use it when a user wants to see what the agent is doing in a browser, step in for logins or CAPTCHAs, or preview a web app the agent is building.
The browser shares the worker's files with the agent's shell and file tools.
Agents with the [`chat_ui`](chat-ui.md) toolkit can open the panel for the user; see [Control the browser, then show it](chat-ui.md#control-the-browser-then-show-it) for that call.

## Requirements and opt-in

Worker Computer needs a dedicated **Docker** or **Kubernetes** worker running the current MindRoom worker image.
The `static_runner` backend and local process execution do not support it.
It is disabled by default.

Set these values on the **primary MindRoom runtime**, alongside the normal worker settings from [Sandbox Workers](../deployment/sandbox-proxy.md):

```dotenv
MINDROOM_WORKER_BACKEND=docker
MINDROOM_DOCKER_WORKER_IMAGE=mindroom:dev
MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer
MINDROOM_WORKER_COMPUTER_ENABLED=true
MINDROOM_COMPUTER_ALLOWED_ORIGINS=["https://chat.example.org"]
MATRIX_HOMESERVER=https://matrix.example.org
MATRIX_SERVER_NAME=matrix.example.org
```

`MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer` is required on Docker; see [Browser and container sandboxing](#browser-and-container-sandboxing).
For Docker development, build the image with:

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .
```

Give the agent exactly one worker-routed browser provider, `browser` or [`browser_mcp`](#native-playwright-mcp-provider), and an effective `user_agent` worker scope, so the browser and shell share one worker per requester and agent.
Shared agents set `worker_scope: user_agent` (directly or through `defaults.worker_scope`); [private agents](../configuration/agents.md#private-instances) set `private.per: user_agent` instead, because they cannot also set `worker_scope`:

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Research with a browser and save results.
    model: default
    tools: [browser, shell, file]
    worker_tools: [browser, shell, file]
    worker_scope: user_agent
```

Keep the `browser` tool's default `target: host`; the connected-user `target: desktop` is a separate feature and does not run in the Computer.
Browser URL restrictions still apply, so private networks stay blocked unless allowed.
The `browser` tool uses the worker image's bundled Chromium in a Computer unless `BROWSER_EXECUTABLE_PATH` is set.
For worker egress proxies, see [Egress Proxy for Browsers](web-scraping-and-browser.md#egress-proxy-for-browsers); the same rules apply to both browser providers.

With the runtime Helm chart:

```yaml
workers:
  backend: kubernetes
  computerEnabled: true
env:
  extra:
    - name: MINDROOM_COMPUTER_ALLOWED_ORIGINS
      value: '["https://chat.example.org"]'
```

Use the chart's existing worker image, authentication secret, storage, and RBAC settings.
The hosted instance chart runs no dedicated workers, so Computer is available only through the runtime chart or direct Docker and Kubernetes worker deployments.
Changing `MINDROOM_WORKER_COMPUTER_ENABLED` replaces existing workers; worker files and browser profiles are kept.

### Preview a local web app

The Computer browser can open a web server that the agent's shell started in the same worker.
Bind the server to `127.0.0.1` and open its URL, for example `http://localhost:5173`, with the browser tool.
The user sees the app in the Computer panel without any public port or preview URL.

This works with both `browser` and `browser_mcp` in a Computer, without `allow_private_networks`.
Use `localhost` or a literal loopback address; aliases such as `localhost.localdomain` are treated as private network names and stay blocked.
Other private addresses and cloud metadata endpoints remain blocked by default.
Pages opened in the Computer can reach any service listening on the worker's loopback interface, as in a local development browser.

## Connect MindRoom Chat

Set the trusted origin of the MindRoom runtime API, without a path, in Chat's operator-managed `config.json`:

```json
{
  "mindroom": {
    "computers": {
      "apiUrl": "https://computer.example.org"
    }
  }
}
```

The shipped value is empty, which hides the Computer action.
Remote origins require HTTPS; loopback HTTP works for local development.
Chat shows the Computer action when this origin is set and the room has a joined MindRoom agent; with several agents, the user picks one first.
Point `apiUrl` at the runtime that owns the agent's workers; a host that only serves Matrix, Chat, or local provisioning (`/v1/local-mindroom/*`) does not serve computers.

On the runtime, `MINDROOM_COMPUTER_ALLOWED_ORIGINS` is a JSON list of exact Chat origins allowed to open computers.
For the bundled MindRoom Chat iOS app, explicitly add `"capacitor://localhost"` alongside the trusted HTTPS web origins, for example `["https://chat.mindroom.chat", "capacitor://localhost"]`.
Other custom-scheme origins and opaque `"null"` origins are refused.
The iOS app uses Matrix OpenID and computer session tokens; it does not need web sign-in cookies.
Each entry must be a scheme and host with an optional port, with no path or wildcard, and HTTP is allowed only for `localhost` and literal loopback addresses.
One invalid entry disables the whole list.

Opening a computer requires that the requester and the agent are both joined to the room, that the agent's [access policy](../authorization.md#responder-access) lets the requester use it, and that the requester belongs to the configured Matrix server.
A session lasts at most one hour.
Each requester can hold up to eight concurrent viewer sessions, and a runtime up to 256; closing a viewer frees its slot.

### Reverse proxy and host routing

Route `/api/computers/*`, including WebSocket upgrades, to the **MindRoom runtime API**.
Preserve the `Origin` and `Sec-WebSocket-Protocol` headers and allow long-lived WSS connections.
These routes use their own session authentication, separate from dashboard authentication.
Do not log bearer tokens or WebSocket subprotocols.

For example, a dedicated Caddy origin pointing to a runtime on the same host:

```caddy
computer.example.org {
    reverse_proxy /api/computers/* 127.0.0.1:8765
}
```

Use your actual runtime address and port.
Expose only this gateway; the worker's display has no public listener.

## Watch, take control, and resume

<video controls playsinline preload="metadata" aria-label="A user watches the agent's browser, takes control, and hands it back" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/63868e17-6824-4073-8904-e1b7abc4b173#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/cc079b2f-6dbf-4509-9fdc-4ec21da85d7a#t=0.1" type="video/mp4">
</video>

- **Watch** shows the browser without input rights.
- **Take control** waits for the agent's current browser action to finish, then gives input to one viewer.
  While the user holds control, the agent's browser calls return a blocked result; background shell processes keep running.
- **Resume agent** releases control, returns the screen to watch mode, and sends one ordinary message mentioning the agent in the originating room or thread.
  If that message fails to send, Chat shows the error and does not retry.
- **Close** disconnects this viewer and releases its control; the browser and files stay for later work.
- **Stop** closes the browser and display and ends the session.
  **Start computer** creates a fresh session; profiles and files remain.

Changing room, thread, account, or selected agent closes the old viewer.
On desktop the panel sits beside the conversation and closes the Members drawer; on mobile it fills the screen and has a close button.
Keyboard input over a controlled screen goes to the browser, not the composer.

The browser keeps running between tool calls, including tabs the user opened.
When the agent focuses a tab or acts on a page, that page comes to the foreground, so the agent's screenshots and snapshots show what the viewer sees.
The `browser` provider identifies tabs by stable target IDs, while `browser_mcp` uses tab indices that can change.

## Native Playwright MCP provider

`browser_mcp` gives the agent native Playwright tools such as `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_evaluate`, `browser_tabs`, `browser_take_screenshot`, `browser_file_upload`, and `browser_pdf_save` instead of the action-based `browser` tool:

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Research with a browser and save results.
    model: default
    tools: [browser_mcp, shell, file]
    worker_scope: user_agent
```

`browser_mcp` runs in workers by default and works only in an enabled Computer.
It uses the Playwright MCP server and Chromium bundled in the worker image, with vision and PDF capabilities.
The agent cannot install packages, change the server or its settings, run server-side code with `browser_run_code_unsafe`, install browsers, or change request routing.
`browser_evaluate` runs JavaScript in the page only.
Service workers are blocked.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `allow_private_networks` | `boolean` | `false` | Allow trusted private destinations; metadata and link-local addresses stay blocked. |

```yaml
agents:
  researcher:
    tools:
      - browser_mcp:
          allow_private_networks: true
    worker_scope: user_agent
```

Screenshots without a `filename` are shown to the model and also saved in the workspace; passing a `filename` saves without showing.
Automatic screenshots, PDFs, and downloads go to `browser/` in the workspace, and explicit filenames resolve relative to the workspace, so use names like `browser/page.png` to keep them together.
On later turns, open a saved image with `view_file(path=...)` from the `attachments` toolkit.
Captures are never posted to Matrix automatically.
`mindroom_output_path` can save text results to a file but not images.
Upload and drop paths must name existing regular files inside the worker workspace.

List tabs again before selecting one by index.
`browser_resize` changes the page viewport, not the window size.
Chromium may show a banner about resolver flags; it is expected.

## Storage and lifetime

Browser profiles live under `browser-profiles/<profile>` in the dedicated worker's persistent storage root; `browser_mcp` uses `browser-profiles/native-mcp`.
Completed downloads land in the workspace's `browser/` directory, where the shell and file tools can read them; for `browser`, a custom [`output_dir`](web-scraping-and-browser.md#browser-configuration) replaces that directory.
The `browser` provider adds a unique prefix to download filenames; `browser_mcp` keeps the native name.
Profiles and files survive stopping the computer and recreating the worker while the storage volume remains.
Stopping the computer does not sign out of sites or reset the profile.

To reset a profile, stop the computer and stop or recycle its dedicated worker.
Remove only that worker's `browser-profiles/<profile>` directory from its persistent storage; the next browser action creates a new profile.
Do not remove the whole worker storage root unless you also intend to delete its files.

An open viewer keeps the worker from idling out.
After the last viewer disconnects, the worker's normal idle timeout applies: `MINDROOM_DOCKER_WORKER_IDLE_TIMEOUT_SECONDS` in [Sandbox Workers](../deployment/sandbox-proxy.md#dedicated-docker-worker-backend) or `workers.kubernetes.idleTimeoutSeconds` in [Kubernetes Deployment](../deployment/kubernetes.md#worker-settings).
Closing the panel does not stop the worker immediately.

## Browser and container sandboxing

Chromium always runs with its Linux process sandbox enabled.
If the sandbox cannot start, the browser call fails instead of running unsandboxed.

### Docker

`MINDROOM_DOCKER_WORKER_SECURITY_POLICY` accepts `runtime_default` (the default) or `computer`.
`computer` drops all Linux capabilities, sets `no-new-privileges`, and applies a packaged seccomp profile that adds only the namespace and `chroot` operations Chromium's sandbox needs to Docker's default policy.
It applies to every dedicated Docker worker, whether or not Computer is enabled or the agent uses a browser.
Changing the policy replaces existing workers.

Configuration fails with `MINDROOM_WORKER_COMPUTER_ENABLED=true requires MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer.` when Computer is enabled under `runtime_default`.
Computer workers must also run as a non-root user.
`MINDROOM_DOCKER_WORKER_USER=root` or UID 0, including a runtime that itself runs as root and passes its UID to workers, fails configuration, and a worker that starts as root refuses requests.
An empty `MINDROOM_DOCKER_WORKER_USER` uses the image's default user.

### Kubernetes

Kubernetes workers use the pod's `RuntimeDefault` seccomp policy.
If that policy allows unprivileged user namespaces, nothing more is needed.
Otherwise, install the packaged profile `src/mindroom/workers/backends/seccomp/worker-computer.json` on every node that can run workers, under a versioned filename such as:

```text
/var/lib/kubelet/seccomp/profiles/worker-computer-578ef2b662d8e9a8.json
```

Verify its SHA-256 on every node: `578ef2b662d8e9a886132148a0b75f0388efff92ed902b16d7be2777ae3788fa`.
Kubernetes only names the file, so it cannot check the contents for you.
When the profile changes, install it at a new versioned path on all nodes before switching to it.

Select it for the worker container:

```dotenv
MINDROOM_KUBERNETES_WORKER_SECCOMP_PROFILE_JSON={"type":"Localhost","localhostProfile":"profiles/worker-computer-578ef2b662d8e9a8.json"}
```

The value must contain exactly `type: Localhost` and a relative `localhostProfile` path.
With the runtime chart, set the same object at `workers.kubernetes.seccompProfile`:

```yaml
workers:
  kubernetes:
    seccompProfile:
      type: Localhost
      localhostProfile: profiles/worker-computer-578ef2b662d8e9a8.json
```

This applies only to the main worker container; init and extra containers keep `RuntimeDefault`.
A node without the profile fails pod startup with `CreateContainerError`.
The node kernel and its security modules must also allow unprivileged user namespaces.

## Troubleshooting

| Symptom or error | Fix |
| --- | --- |
| No Computer action in Chat | Set `mindroom.computers.apiUrl` in Chat's `config.json` and make sure an agent is joined to the room. |
| `Computer stream origin is not allowed.` | Add Chat's exact origin to `MINDROOM_COMPUTER_ALLOWED_ORIGINS` and check every entry is valid. |
| `Requester and agent must both be joined to this room.` or `Requester cannot use this agent.` | Check room membership and the agent's access policy. |
| `Computer requires exactly one browser provider and explicit user_agent worker scope.` | Give the agent exactly one of `browser` or `browser_mcp` and an effective `user_agent` scope: `worker_scope: user_agent`, or `private.per: user_agent` for private agents. |
| `Computer requires browser tools routed to the worker.` | Add the browser provider to `worker_tools`. |
| `Computer requires a dedicated Docker or Kubernetes worker backend.` | Set `MINDROOM_WORKER_BACKEND` to `docker` or `kubernetes`. |
| `Worker computer is not enabled on this dedicated worker.` | Set `MINDROOM_WORKER_COMPUTER_ENABLED=true` on the primary runtime. |
| `Computer requester session capacity reached.` or `Computer session capacity reached.` | Close other viewers or wait for sessions to expire. |
| `...create a new session.` | Access, configuration, or the worker changed, or the session expired; reopen the panel. |
| Empty desktop after startup | Ask the agent to open a page. |
| WebSocket fails | Check the gateway's TLS, WebSocket upgrades, and routing to the runtime. |

Use **Reconnect** after a transient stream failure.
If the browser stops responding, recycle the dedicated worker; profiles and files stay in persistent storage.
