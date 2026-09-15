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

Interactive sessions require effective `worker_scope: user_agent` and worker-routed `browser`.
The normal requester/agent worker resolver supplies the same worker to Chat and tool calls.
Keep the browser's default `target: host` for this managed worker browser.
The connected-user `target: desktop` is a separate feature and is not supported inside the worker computer.
Browser URL restrictions still apply; enabling a display does not enable access to private networks.

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
The worker token stays between the backend and worker.

### Reverse proxy and host routing

Route `/api/computers/*`, including WebSocket upgrades, to the **MindRoom runtime API**.
Preserve the `Origin` and `Sec-WebSocket-Protocol` headers and allow long-lived WSS connections.
Use the explicit `MINDROOM_COMPUTER_ALLOWED_ORIGINS` list for both HTTP CORS and WebSocket origin checks.
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

The browser process persists across runner requests, including stable target IDs and native tabs opened by the user.
When the agent selects a managed tab with focus or a page action, Chromium brings that page to the visible foreground.
A tool's screenshot or snapshot therefore observes the page shown in the viewer.

## Storage and lifetime

Browser profiles live under `browser-profiles/<profile>` in the dedicated worker's persistent storage root.
Completed downloads are copied into the agent workspace's `browser/` directory with a unique filename prefix, so the shell/file tools can read them.
These files survive computer stop/restart and worker recreation while the storage volume remains.
Stopping the computer is not a sign-out or profile reset.

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
Use `--chromium <executable>` if host Chromium is not discoverable.
The probe verifies same-target reuse, framebuffer pixels, rejected watch input, takeover/agent blocking, native-tab focus/navigation, downloads through shell, stop/restart persistence, and requester isolation.

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
