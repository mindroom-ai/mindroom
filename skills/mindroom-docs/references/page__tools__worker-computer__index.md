# Worker Computer

Worker Computer shows the Chromium browser that an agent uses inside its dedicated worker.
MindRoom Chat can watch that screen, take control, resume the agent, and stop the computer.
Browser and shell tools share the worker's files.

## Requirements and opt-in

Use a dedicated **Docker** or **Kubernetes** worker with the current MindRoom worker image.
Shared static runners and local process execution do not support interactive computers.
The feature is disabled by default; existing headless and connected-user desktop browser behavior stays unchanged.

Set these values on the **primary MindRoom runtime**:

```dotenv
MINDROOM_WORKER_BACKEND=docker
MINDROOM_DOCKER_WORKER_IMAGE=mindroom:dev
MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer
MINDROOM_WORKER_COMPUTER_ENABLED=true
MINDROOM_COMPUTER_ALLOWED_ORIGINS=["https://chat.example.org"]
MATRIX_HOMESERVER=https://matrix.example.org
MATRIX_SERVER_NAME=matrix.example.org
```

Keep the normal worker authentication/storage configuration from [Sandbox Workers](https://docs.mindroom.chat/deployment/sandbox-proxy/).
For Docker development, build the existing image:

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .
```

Route the browser and shell to the same requester-and-agent scope:

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

Interactive sessions require effective `worker_scope: user_agent` and exactly one worker-routed browser provider: `browser` or `browser_mcp`.
The normal requester/agent worker resolver supplies the same worker to Chat and tool calls.
Keep the browser's default `target: host` for this managed worker browser.
The connected-user `target: desktop` is a separate feature and is not supported inside the worker computer.
Browser URL restrictions still apply; enabling a display does not enable access to private networks.

### Preview a local web app

The Computer browser can open a web server started by the agent's shell in the same worker.
Start the server bound to `127.0.0.1`, then open its URL, for example `http://localhost:5173`, with the browser tool.
The user sees the app in MindRoom Chat's Computer panel; no public port or preview URL is required.

This loopback access is automatic for both `browser` and `browser_mcp` when bound to a dedicated Computer display.
Use `localhost` or a literal loopback address for previews; ordinary DNS aliases such as `localhost.localdomain` retain the existing private-network policy.
Other private addresses and cloud metadata endpoints remain blocked by default.
Headless workers, unbound browsers, connected desktops, and ordinary server-side HTTP tools retain their existing policies.
As with a local development browser, pages opened in Computer mode can make requests to services listening on the same worker's loopback interface.

Without a configured upstream proxy, both providers enforce destinations through a worker-local proxy, including redirects.
When the worker sets `all_proxy` or `ALL_PROXY` to an HTTP(S) proxy without embedded credentials, Chromium uses that proxy directly for external connections; only loopback bypasses it.
Matching `http_proxy` and `https_proxy` settings are also supported.
Per-scheme or automatic proxy configurations that cannot be preserved are rejected with a configuration error rather than silently bypassed; use `all_proxy` for these workers.
The upstream proxy is trusted to resolve destination hostnames and enforce its own network restrictions, including blocking private and metadata addresses.
Browser URL validation still requires hostnames to resolve inside the worker; proxy-only DNS names are not supported.
This keeps domain-based firewall rules intact.
A rejected proxy connection never falls back to a direct connection.

For the runtime Helm chart:

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
The instance chart names its opt-in values `workerBackend: kubernetes` and `workerComputerEnabled: true`; supply the allowed-origin environment value to its primary runtime through your deployment's environment configuration.
Changing the feature flag changes the worker configuration signature, so workers are replaced as needed.

## Native Playwright MCP provider

Select `browser_mcp` for native Playwright tools such as `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_evaluate`, `browser_tabs`, and `browser_take_screenshot`:

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Research with a browser and save results.
    model: default
    tools: [browser_mcp, shell, file]
    worker_scope: user_agent
```

`browser_mcp` routes to workers by default and requires an enabled dedicated Computer worker.
Assign exactly one browser provider to this scope.
The existing `browser` action-based provider and connected user desktop extension remain available separately.

The full image installs `@playwright/mcp@0.0.78` with fixed `vision,pdf` capabilities at build time.
The native provider uses that server's bundled Chromium `151.0.7922.10` (revision `1232`), installed under root-owned `/opt/mindroom-browser-mcp` with a fixed executable path and `browser-version.json` build metadata.
Update and test the server and bundled browser together, including a fresh download after reopening a saved profile.
System Chromium 152 can crash during that sequence with the pipe transport; the bundled browser preserves the existing sandbox, persistent profiles, and stdio process architecture.
The action-based `browser` provider also defaults to this bundle when bound to a Computer worker display.
Its existing operator `BROWSER_EXECUTABLE_PATH` override remains supported; custom browser versions need the same restart/download checks.
Ordinary headless and connected desktop browser selection is unchanged.
The native schemas are pinned and checked against the installed server on connection.
Runtime calls cannot install packages, choose another server, add capabilities, or change launch/init settings.
`browser_run_code_unsafe`, browser installation, and route mutation tools are excluded.
Page-context `browser_evaluate` remains available; it does not expose server-side Node APIs.

The browser runs visibly on its worker display with sandboxing enabled.
Its profile persists under the worker storage root at `browser-profiles/native-mcp`; automatic screenshots, PDFs, and downloads use `browser/` inside the shared workspace.
Explicit native filenames resolve relative to the workspace; use `browser/native.png` or `browser/native.pdf` to keep named files in that directory.
Upstream workspace file restrictions remain enabled.
File upload and drop paths are resolved before dispatch and must name existing regular files inside the canonical worker workspace.
This rejects symlinks to files outside the workspace while allowing symlinks to files inside it.
This path check does not protect against concurrent filesystem changes by malicious shell code.
Screenshots without a native `filename` return inline model-visible images; a native filename produces the upstream file result instead.
MindRoom's `mindroom_output_path` redirects text; redirecting media returns the established unsupported-media receipt.
The primary never fetches worker paths to reconstruct images.

Native tab indices can change; list tabs again before selecting one.
The native `browser_resize` tool changes the page viewport, while the operating-system window can retain its previous size.
The pinned upstream server can display Chromium's resolver-flag banner when using the required destination proxy.
Worker startup still requires Chromium sandboxing and the configured container restrictions.

All native calls, including snapshots and browser close, use the same takeover gate as the existing provider.
Timeout, cancellation, and server failure close owned resources before replacement.
On Linux, a dedicated child supervisor reaps detached Chromium descendants as well as the MCP server.

The packaged init-page hook checks intercepted requests through an authenticated worker-loopback verifier.
Private networks are denied by default.
To allow trusted private destinations, configure:

```yaml
agents:
  researcher:
    tools:
      - browser_mcp:
          allow_private_networks: true
    worker_scope: user_agent
```

Metadata and link-local addresses stay blocked even with this option.
Without a configured upstream proxy, a worker-local destination proxy checks HTTP(S) connections, including redirect destinations and loopback, then connects to the validated numeric address.
With an upstream proxy, that proxy owns destination enforcement as described above.
The browser keeps normal TLS, origins, and redirect behavior.
The URL callback also restricts request schemes; callback errors and timeouts deny requests, and service workers are blocked.
This guard does not promise DNS-rebinding protection, coverage of every network protocol, or confinement of malicious shell code.
Persistent workspace files remain intentionally shared with the agent's other worker tools.

## Browser and container sandboxing

Worker computers launch Chromium with its Linux process sandbox enabled.
A startup failure is returned to the caller; MindRoom does not retry with Chromium's sandbox disabled.

Select `MINDROOM_DOCKER_WORKER_SECURITY_POLICY=computer` explicitly on the primary runtime before enabling Computer.
The Docker pool policy accepts only `runtime_default` (the default) or `computer`.
Enabling `MINDROOM_WORKER_COMPUTER_ENABLED=true` with `runtime_default` fails configuration; there is no weaker fallback.
Computer configuration also rejects known root worker identities, including an inherited host UID 0 and explicit `MINDROOM_DOCKER_WORKER_USER=root` or numeric UID 0 overrides.
An empty worker-user setting uses the image default; custom images and named accounts must resolve to a non-root UID.
The Computer worker also checks its actual effective UID at startup and refuses to serve requests as root.
The `computer` policy drops all Linux capabilities, sets `no-new-privileges`, and uses the packaged `src/mindroom/workers/backends/seccomp/worker-computer.json` profile.
It applies to the entire Docker worker pool, even with Computer disabled or agents that do not select a browser tool.
Computer availability controls lazy browser/display startup separately from the pool's security policy.
With Computer disabled and `runtime_default` selected, ordinary Docker workers retain their previous launch settings and compatible configuration identities; upgrading alone does not replace them for this policy.
The reviewed profile is based on Moby's maintained default policy and adds only the namespace and `chroot` operations used by the Chromium sandbox.
Workers under the `computer` policy are replaced if their actual host security settings differ from the required policy.
Changing the selected policy reconciles workers to the new configuration; ordinary image, authentication, configuration and mount changes still trigger their existing reconciliation.

Kubernetes workers keep the pod-level `RuntimeDefault` seccomp policy.
If that policy supports unprivileged user namespaces, no extra setting is needed.
Runtimes that block Chromium's namespace sandbox need the packaged profile installed on every eligible node at:

```text
/var/lib/kubelet/seccomp/profiles/worker-computer-578ef2b662d8e9a8.json
```

Use immutable, versioned filenames for node profiles; this example uses the reviewed profile's SHA-256 prefix.
Verify the installed bytes on every eligible node against the packaged profile's SHA-256:
`578ef2b662d8e9a886132148a0b75f0388efff92ed902b16d7be2777ae3788fa`.
A `localhostProfile` path only names a node file; MindRoom cannot verify its contents through that string.
On profile changes, install verified bytes at a new versioned path on all eligible nodes before changing the selected path.

Then select it for the main worker container:

```dotenv
MINDROOM_KUBERNETES_WORKER_SECCOMP_PROFILE_JSON={"type":"Localhost","localhostProfile":"profiles/worker-computer-578ef2b662d8e9a8.json"}
```

The runtime chart accepts the same object at `workers.kubernetes.seccompProfile`; the instance chart uses `kubernetesWorkerSeccompProfile`.
For example:

```yaml
workers:
  kubernetes:
    seccompProfile:
      type: Localhost
      localhostProfile: profiles/worker-computer-578ef2b662d8e9a8.json
```

Kubernetes applies this override only to the main worker container.
Init and extra containers continue to inherit the pod's `RuntimeDefault` policy.
A missing node profile causes pod startup to fail with `CreateContainerError`, which keeps the sandbox requirement explicit.
The node kernel and its security modules must also permit unprivileged user namespaces.

The Chromium process sandbox, the container runtime's seccomp filter, and the persistent worker filesystem address different boundaries.
The browser sandbox isolates Chromium processes, seccomp limits host syscalls, and persistent storage deliberately retains the browser profile and worker files across container replacement.

## Connect MindRoom Chat

Configure an explicitly trusted **origin**, without a path, in Chat's operator-managed configuration:

```json
{
  "mindroom": {
    "computers": {
      "apiUrl": "https://computer.example.org"
    }
  }
}
```

The shipped value is empty.
Remote origins require HTTPS; loopback HTTP works for local development.
Chat does not discover endpoints from room messages or agent output.
It shows the Computer action when this origin is configured and the room has a joined MindRoom agent.
With multiple agents, choose the exact agent before opening its computer.

The backend verifies a fresh Matrix OpenID token against its configured homeserver and checks requester membership, agent membership, and responder access policy.
The Matrix server name must match the configured server.
HTTP session credentials remain in Chat memory; stream tickets are single-use and expire after 30 seconds.
Sessions last at most one hour.
Each verified requester can hold up to eight concurrent viewer sessions, within a process-wide limit of 256.
Closing a viewer or reaching session expiry frees its slot; excess requests are rejected before allocating a worker.
The worker token stays between the backend and worker.

### Reverse proxy and host routing

Route `/api/computers/*`, including WebSocket upgrades, to the **MindRoom runtime API**.
Preserve the `Origin` and `Sec-WebSocket-Protocol` headers and allow long-lived WSS connections.
Use the explicit `MINDROOM_COMPUTER_ALLOWED_ORIGINS` list for both HTTP CORS and WebSocket origin checks.
Allowed browser origins require HTTPS except for localhost and literal loopback IP addresses, which also support HTTP.
These routes use computer-session authentication, separately from dashboard authentication.
Do not log bearer tokens or stream-ticket subprotocols.

For example, a dedicated Caddy origin pointing to a runtime on the same host:

```caddy
computer.example.org {
    reverse_proxy /api/computers/* 127.0.0.1:8765
}
```

Use your actual runtime address and port.
Expose only the gateway; raw VNC uses a worker-local Unix socket and has no public TCP listener.

In the documented MindRoom host layout, the local `mindroom` LXC runs the full `mindroom-lab` and `mindroom-chat` application services.
The production `mindroom-hetzner` host serves Matrix, Chat, bridges, and local provisioning; it does not run the full backend.
Its `/v1/local-mindroom/*` provisioning route does not serve computers.
A Chat deployment on that host must point its computer origin at the actual runtime that owns the agent workers.

## Watch, take control, and resume

- **Watch** opens the existing computer without input rights.
  The worker rejects viewer input even if someone changes noVNC's frontend view-only setting.
- **Take control** waits for an active browser operation to settle and grants input to one connected viewer.
  Managed agent browser calls return a clear blocked result while control is held.
  Background shell processes continue.
- **Resume agent** releases control, reconnects the screen in watch mode, and sends one ordinary message mentioning the selected agent in the originating room or thread.
  A screen reconnect failure does not suppress that message after release.
  A send failure is shown explicitly and is not automatically retried.
- **Close** disconnects only this viewer and releases its control.
  It keeps the browser and files for later work.
- **Stop** closes the browser and display and invalidates the old session.
  **Start computer** creates a fresh session; persisted profiles and files remain.

Changing room, thread, account, or selected agent tears down the old viewer.
On desktop the panel sits beside the conversation and closes the Members drawer.
On mobile it fills the screen and provides a close button.
Keyboard input over the controlled screen stays out of the composer and command palette.

The browser process persists across runner requests, including tabs opened by the user.
The `browser` provider uses stable target IDs; `browser_mcp` exposes the native server's current tab indices.
When the agent selects a managed tab with focus or a page action, Chromium brings that page to the visible foreground.
A tool's screenshot or snapshot therefore observes the page shown in the viewer.

## Storage and lifetime

Browser profiles live under `browser-profiles/<profile>` in the dedicated worker's persistent storage root.
Completed downloads are available in the agent workspace's `browser/` directory, so the shell/file tools can read them.
The `browser` provider adds a unique filename prefix; `browser_mcp` follows native download naming.
These files survive computer stop/restart and worker recreation while the storage volume remains.
Stopping the computer is not a sign-out or profile reset.

When a storage path exceeds Linux's Unix socket limit, the display uses a unique private temporary directory
for its RFB socket. Cleanup removes that directory after the display children are reaped; persistent storage remains in place.

To reset a profile, stop the computer and recycle/stop its dedicated worker first.
Remove only that worker's `browser-profiles/<profile>` directory from its persistent storage, then let the next browser action create a new profile.
Do not remove the whole worker storage root unless you also intend to delete its files.

An active stream refreshes worker activity at least every 30 seconds.
After the last viewer disconnects, normal worker idle cleanup applies:
`MINDROOM_DOCKER_WORKER_IDLE_TIMEOUT_SECONDS` or `MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS`, both defaulting to 1800 seconds.
Closing the panel does not immediately stop the worker.

## Reconnect and recovery

Use **Reconnect** after a transient stream failure.
It requests a new one-use stream ticket; never reuse old tickets.
Expiry, membership/access revocation, a changed worker scope/configuration, backend restart, or worker replacement invalidates old sessions.
Reopen the panel to obtain a new session after restoring access or configuration.

If the screen shows an empty desktop after startup, ask the agent to open a page.
If it shows a permission error, verify room membership, agent access policy, `user_agent` scope, browser worker routing, and the allowed origin.
If the WebSocket fails, verify the gateway's TLS/upgrades and actual runtime routing.

Safe browser cleanup can wait for an unfinished local Playwright driver handshake.
There is no strict browser shutdown deadline.
If that driver wedges, recycle the dedicated worker; persistent storage remains the recovery source.

## Reproduce local acceptance

The committed probe uses real Docker workers, Chromium, Xvnc and noVNC, with controlled local identity/membership for standalone checks.
It creates a fresh output directory, records exact container IDs, and removes only its own containers.
Use persistent local directories for its state and screenshots.

After installing the repository dependencies and Chat's npm dependencies:

```bash
uv sync --all-extras
uv run scripts/test-worker-computer.py --build \
  --output ./worker-computer-results \
  --novnc ../mindroom-chat/node_modules/@novnc/novnc
```

Use `--image <already-built-image>` without `--build` to reuse a local worker image.
Use `--provider browser_mcp` to exercise the native provider, including uploads, inline image decoding through the primary proxy, named image/PDF files, and native typing.
The default remains `--provider browser`.
Use `--chromium <executable>` if host Chromium is not discoverable.
The probe verifies browser-session reuse, framebuffer pixels, rejected watch input, takeover waiting for an active browser call, agent blocking during control, tab focus/navigation, downloads through shell, stop/restart persistence, and requester isolation.
It records the actual worker security settings and Chromium sandbox diagnostics.

For the Chat desktop/mobile spec, make a local Tuwunel image available, start Chat on loopback, then start this fixture:

```bash
uv run scripts/test-worker-computer.py --serve \
  --image mindroom-worker-computer-test:local \
  --matrix-image ghcr.io/mindroom-ai/mindroom-tuwunel:latest \
  --chat-origin http://127.0.0.1:4173 \
  --output ./worker-computer-chat-results
```

This creates an isolated loopback Matrix container, test users, a room/thread, and a gateway.
It writes mode-0600 `chat-fixture.json` with test credentials.
From the Chat checkout, pass that file's location explicitly:

```bash
E2E_COMPUTER_FIXTURE=../mindroom/worker-computer-chat-results/chat-fixture.json \
E2E_BASE_URL=http://127.0.0.1:4173 \
npm run test:e2e -- e2e/worker-computer.spec.ts
```

The spec does not use a default homeserver or real account.
It checks native canvas typing with agent readback, exactly one thread continuation, desktop/mobile placement, close/reopen and stop/start.
Interrupt the fixture with Ctrl+C to remove its workers and Matrix container.
An optional `--matrix-fixture` accepts the same generated Matrix fixture schema for reuse; that externally supplied container remains owned by its creator.
Keep fixture credentials, traces and generated output outside commits.
