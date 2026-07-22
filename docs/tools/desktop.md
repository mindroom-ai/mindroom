---
icon: lucide/monitor-up
---

# Matrix Desktop Bridge

The `desktop` tool lets a cloud-hosted MindRoom agent inspect and operate explicitly allowlisted applications on a local computer without opening an inbound port.
The local computer runs an outbound Matrix sync client, while commands and responses use Olm-encrypted to-device events addressed to exact pinned Matrix devices.
App screenshots are encrypted before upload to Matrix media, and their decryption keys travel only inside the encrypted response.
By default, the screenshot is model-visible and MindRoom does not create a separate plaintext attachment file on the cloud host.
Agno's normal agent-session persistence can retain model-visible screenshot pixels in the session database, so protect and expire that storage according to the sensitivity of the controlled applications.
When a user asks to receive the image, `desktop(action="screenshot", return_attachment=true)` returns a turn-scoped `att_*` handle that can be sent with `matrix_message`.
That handle reuses the existing encrypted Matrix media instead of saving or uploading the screenshot again, and it expires when the turn ends.

The bridge is accessibility-first on macOS.
It returns a bounded accessibility tree with roles, names, values, bounds, writable state, and advertised actions, plus a screenshot cropped to the selected app window.
The agent normally selects an element index from that state instead of guessing a screen coordinate.
Every element index is bound to one opaque `state_id`, and a new state invalidates the old indexes.
The local bridge also pins the exact local process and window, rechecks the current app structure and values before acting, and rejects stale state.
On macOS, a complete tree is sampled until it is briefly stable before its `state_id` is returned, while a capped partial tree is returned immediately with `truncated: true`.

Pixel and keyboard operations remain explicit fallbacks for controls that do not expose useful accessibility elements.
Fallback coordinates run from `0` to `1000` inside the selected app window rather than using raw desktop pixels.
Every completed action normally returns a new accessibility state and a new app-window screenshot for the next step.
If follow-up state or capture fails after an action completes, the tool tells the agent not to repeat the action automatically.
On macOS, taking the scoped screenshot foregrounds the selected application, so observation can change which local window is active.

## Security Model

The local bridge starts in observe-only mode unless a person at the computer grants a short control lease on the command line.
The local process independently checks the exact cloud Matrix user, device ID, Ed25519 fingerprint, human requester ID, agent name, app ID, command expiry, request ID, and monotonic session sequence.
Only applications named with local `--allow-app` options can be listed, launched, inspected, captured, or controlled.
Cloud configuration and model output cannot add an application to that allowlist.
The allowlist restricts the bridge's direct target, but an allowed app can still cause operating-system side effects such as opening a link or document in another app.
The bridge cannot then inspect or control that newly opened app unless its exact app ID is also locally allowlisted.
The local bridge executes only one cloud request at a time, and state binding prevents parallel controls planned from the same state from both succeeding.
Cloud configuration cannot enable control, extend a running lease, or change any local allowlist.
Restarting the local bridge returns it to observe-only mode unless `--allow-control` is supplied again.
Moving the pointer to the upper-left corner triggers PyAutoGUI's emergency stop and latches control off until the local bridge is restarted.
The local process writes an audit log entry for each completed or rejected parsed command without logging action parameters, values, or typed text.

The observation actions are:

- `status` reports coarse screen, cursor, accessibility-backend, and local lease state.
- `list_apps` lists only locally allowlisted app IDs and whether each is running.
- `get_app_state` returns a fresh accessibility state and app-window screenshot.
- `screenshot` returns a fresh state and requires its app-window screenshot to succeed.

Only `screenshot` accepts `return_attachment=true`.
The option does not write plaintext pixels to attachment storage.
It exposes the existing encrypted MXC object only inside the active tool context and does not upload the image again.
The media key is protected by the room event in an E2EE room and has the same visibility as other event content in an unencrypted room.

The control actions are:

- `launch_app` launches or foregrounds one exact locally allowlisted application.
- `click_element` invokes the selected element's semantic press action.
- `set_value` changes an accessibility value only when the selected element reports that it is writable.
- `scroll_element` scrolls at the selected element's current bounds.
- `perform_action` invokes an exact action advertised by the selected element.
- `click` clicks a normalized fallback coordinate inside the app window.
- `type_text` types up to 2,000 characters into the validated and focused app.
- `scroll` scrolls a bounded number of pages at the app or an optional normalized app coordinate.
- `keypress` presses one locally safe navigation key in the validated app.

The bridge does not expose a shell, filesystem, clipboard, microphone, webcam, unlock operation, privilege elevation, or arbitrary local RPC.
It operates only the currently logged-in graphical session and cannot bypass operating-system permission prompts.

The optional Playwright MCP extension path is a separate browser capability on the same pinned Matrix transport and local control lease.
It returns semantic page snapshots and stable element references from the browser, which are usually more precise for forms and interactive websites than desktop coordinates.
Its observation actions are `status`, `profiles`, `tabs`, `snapshot`, `screenshot`, and `console`.
Its supported control actions are `start`, `stop`, `open`, current-tab `close`, `navigate`, `pdf`, `upload`, `dialog`, and `act`.
The browser `act` action supports semantic click, type, key press, hover, drag, select, multi-field fill, resize, wait, evaluate, and close operations.
Enabling the extension grants access to the tabs and signed-in state in the connected browser profile, and the desktop application allowlist does not narrow that access to one tab or origin.
Extension mode is not a network sandbox: MindRoom validates URLs passed directly to `open` and `navigate`, but redirects, page scripts, and evaluated JavaScript retain the connected profile's normal network reach.
The local MCP process is pinned to the documented package version and can read upload files only from its `<storage>/desktop-browser` workspace.
The desktop browser target operates the extension's current tab and rejects `targetId` and `focus`, because Playwright MCP exposes mutable numeric tab indices that can point at a different signed-in tab after another tab closes.
The browser extension and MCP process communicate over a machine-local loopback connection, while every cloud-to-local command still travels through pinned Matrix Olm encryption.

Matrix protects the local-to-cloud transport, but accessibility state and screenshots become model input after MindRoom decrypts them in the cloud process.
Accessibility APIs can expose labels, document text, form values, and other semantic content that is not obvious from the screenshot alone.
The macOS backend recognizes secure text fields from both accessibility roles and subroles, suppresses their values, and refuses to change them semantically.
Your configured model provider can therefore receive both the returned accessibility fields and visible app contents.
Desktop action arguments can also appear in model context, approval cards, and MindRoom tool traces, so never use `set_value` or `type_text` for passwords, tokens, recovery codes, or other secrets.
App screenshots, labels, document values, and web content are untrusted data and must never be treated as user authorization or instructions.
Allowlisting a browser grants the bridge access to whichever browser window and tab is selected, not to one origin or website.
The Matrix homeserver can observe routing metadata, timing, and encrypted media size, but not the command body or screenshot plaintext.

## Requirements

Use a dedicated Matrix account for password login whenever your homeserver permits one.
On an SSO-only homeserver, the bridge instead adds a dedicated device to the account selected in the browser.
The desktop account and the cloud MindRoom entity must use the same Matrix federation environment and must be able to exchange to-device events and media.
If the optional local desktop dependencies are missing, `mindroom desktop run` auto-installs the `desktop` extra at startup unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
To install them in advance on the computer being controlled:

```bash
uv tool install 'mindroom[desktop]'
```

macOS supports native semantic state through AXUIElement and requires Accessibility permission for state and control.
macOS also requires Screen Recording permission for screenshots.
Windows and Linux currently expose screenshot-only observation and state through the explicit `primary-screen` app ID, while coordinate input through PyAutoGUI is available during a control lease.
Linux pixel operation currently targets an active X11 desktop because PyAutoGUI does not provide native Wayland control.
A headless or locked graphical session is not a supported target.
Playwright extension mode requires Node.js 18 or newer, a Chromium-family browser, and the official Playwright MCP Bridge extension installed in the browser profile that MindRoom will use.
Chrome and Brave are supported by the local command through an explicit browser executable and user-data root.

## 1. Create the Local Desktop Device

For password login, create a dedicated Matrix user such as `@my-laptop:example.org` using your normal Matrix administration or registration flow.
By default, the login command checks the homeserver's advertised methods.
It uses password login when available and otherwise opens Matrix SSO in your browser.

For password login, pass the dedicated user ID and enter its password at the hidden prompt:

```bash
mindroom desktop login \
  --user-id @my-laptop:example.org \
  --homeserver https://matrix.example.org
```

For an SSO-only homeserver, omit `--user-id` and complete authentication in the browser:

```bash
mindroom desktop login \
  --homeserver https://matrix.example.org
```

You may still pass `--user-id` during SSO as an identity check; MindRoom attempts to revoke and always rejects a newly issued session if SSO returns a different Matrix user.
Use `--login-method sso` to force SSO when the homeserver also advertises password login.
If the homeserver exposes multiple SSO providers, pass its provider ID with `--sso-idp ID` to select one directly.
Use `--no-open-browser` to print a URL that you can open in the browser of your choice.
The browser returns a short-lived, single-use Matrix login token to an unguessable callback path on `127.0.0.1`.
MindRoom exchanges that token immediately and never writes it to disk or terminal logs.
It does not create or replace account-level cross-signing keys for an SSO account; the bridge authenticates this transport by pinning the exact device ID and Ed25519 key shown after login.

The session JSON saves only the reusable Matrix access token and device identifiers.
The device's private Olm identity is persisted separately under `<storage>/encryption_keys/`; on Unix, MindRoom forces its per-user directory to mode `0700`.
For legacy shared-agent setup, the printed Ed25519 value is the corresponding public fingerprint and is persisted only when you copy it into the cloud agent configuration.
On Unix, the session file is forced to mode `0600`, and the bridge refuses to load it if group or other users can read it.
The command prints values similar to these:

```text
User: @my-laptop:example.org
Device: ABCDEFGHIJ
Ed25519: desktop-device-fingerprint
```

Keep these public identity values available for legacy shared-agent configuration.
Private multi-user agents register them through the chat flow below instead.

### Homeservers Behind an Identity-Aware Proxy

A command-line Matrix client cannot reuse an interactive browser proxy cookie.
For a homeserver protected by Cloudflare Access, install the [`cloudflared` CLI](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) and enable interactive Access authentication during desktop login:

```bash
mindroom desktop login \
  --homeserver https://matrix.example.org \
  --cloudflare-access
```

`cloudflared` opens the browser when the local Access session needs authentication.
MindRoom passes the resulting user-scoped JWT as `cf-access-token` on Matrix requests without saving that token in the desktop session.
The session remembers that Cloudflare Access is enabled, so later `mindroom desktop run` commands do not need the flag again.
MindRoom reads the JWT's documented `exp` claim and reuses it only while current.
After expiry, the first Matrix request obtains a current token from `cloudflared`, prompting for browser reauthentication when needed.
For an older saved session, `mindroom desktop run --cloudflare-access` enables Access for that invocation without rewriting the session.

For unattended proxies that issue machine credentials instead, put their required request headers in a separate JSON file:

```json
{
  "X-Access-Client-Id": "client-id",
  "X-Access-Client-Secret": "client-secret"
}
```

Use the exact header names issued by the proxy, restrict the file to its owner, and pass it during both login and bridge startup:

```bash
chmod 600 ~/.config/mindroom/matrix-http-headers.json

mindroom desktop login \
  --user-id @my-laptop:example.org \
  --homeserver https://matrix.example.org \
  --matrix-http-headers-file ~/.config/mindroom/matrix-http-headers.json
```

The file must contain one JSON object whose keys and values are strings.
MindRoom refuses group-readable or world-readable header files on Unix.
The headers apply to every Matrix request made by the desktop client, including login, sync, encryption-key, and media requests.
They are not copied into the saved Matrix session.
Set `MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE` to the file path instead of repeating the option on both commands.
During Matrix SSO, the browser handles any interactive proxy authentication while the header file authenticates the CLI's login-method discovery, token exchange, and later Matrix requests.
Configure the proxy to accept these machine credentials only for the Matrix endpoints the desktop device needs, while keeping normal Matrix authentication enabled.
Do not combine `--cloudflare-access` with a static `cf-access-token` header.

## 2. Configure the Cloud Agent

### Private Multi-User Agents Without Dashboard Access

Configure the Desktop tool on a private agent without authored device identity fields:

```yaml
agents:
  computer:
    display_name: Computer Agent
    role: Operate my locally authorized applications one step at a time
    private:
      per: user_agent
    tools:
      - desktop:
          defer: true
          timeout_seconds: 30
```

Each user then runs `!desktop setup` in a private room containing only that user, the serving bot, and that private Desktop-enabled agent.
The short-lived pairing code is a bearer secret, so MindRoom rejects `!desktop` when any other room member is present.
The serving bot returns a `mindroom desktop pair` command containing a short-lived code and the exact pinned cloud controller identity.
Run that command on the computer after `mindroom desktop login`, then copy the exact `!desktop confirm <code> <verification>` command it prints back to the same Matrix chat.
The claim travels as an authenticated Olm-encrypted to-device event, and confirmation stores the local device identity only in that requester's agent-scoped credential store.
The verification value binds confirmation to the claimed Ed25519 key, so a different device cannot pre-claim a visible pairing code and be confirmed accidentally.
Another requester or agent cannot confirm or use that record, and shared credentials are never used as a fallback.
Until pairing is confirmed, Desktop calls return a setup-required result while the agent's unrelated tools continue to work.
The Desktop tool can explain the trusted chat command, but the model cannot register or replace a device itself.
Use `!desktop rotate` to replace a device without dropping the current target before confirmation, or `!desktop disconnect confirm` to remove it.

### Shared or Operator-Managed Agents

Start cloud MindRoom at least once so the chosen agent has a persistent Matrix device.
On the cloud server, print that controller's local device identity:

```bash
mindroom desktop controller --entity computer
```

Copy the printed controller user, device, and Ed25519 values to the local run command in the next section.
Configure the local desktop device as an authored override on the exact cloud agent that will call the tool:

```yaml
agents:
  computer:
    display_name: Computer Agent
    role: Operate my locally authorized applications one step at a time
    tools:
      - desktop:
          device_user_id: "@my-laptop:example.org"
          device_id: "ABCDEFGHIJ"
          device_ed25519: "desktop-device-fingerprint"
          timeout_seconds: 30
```

The `desktop` tool runs in the primary agent process because it needs that live agent's Matrix device and room requester identity.
The `browser` tool remains worker-routable for host-browser isolation, but its Matrix desktop target requires the primary process's live Matrix context.
Do not list `browser` in `worker_tools` for an agent that uses `target: desktop`; a worker-routed desktop call fails closed with a live-context error.
It is hidden from OpenAI-compatible API runs when approval policy requires Matrix approval because those runs have no Matrix approval transport.

To use the same device with the `browser` tool, configure its desktop target on that agent:

```yaml
agents:
  computer:
    tools:
      - browser:
          default_target: desktop
          device_user_id: "@my-laptop:example.org"
          device_id: "ABCDEFGHIJ"
          device_ed25519: "desktop-device-fingerprint"
          timeout_seconds: 90
```

You can keep `default_target: host` and pass `target="desktop"` only for calls that should use the user's existing local profile.

## 3. Choose the Local App Allowlist

On macOS, use exact application bundle identifiers such as `com.apple.TextEdit` or `com.brave.Browser`.
You can inspect an installed app bundle without granting MindRoom access to it:

```bash
mdls -name kMDItemCFBundleIdentifier /System/Applications/TextEdit.app
```

Add only the applications needed for the current task.
Use the special app ID `primary-screen` only when full-primary-screen observation and coordinate fallback are intentionally required.
`primary-screen` has no semantic elements.
On Windows and Linux, `primary-screen` is currently the only usable state target.

## 4. Run the Local Bridge

Start with observation only and an exact app allowlist:

```bash
mindroom desktop run \
  --controller-user-id @computer:example.org \
  --controller-device-id CLOUDDEVICE \
  --controller-ed25519 cloud-device-fingerprint \
  --allow-requester @alice:example.org \
  --allow-agent computer \
  --allow-app com.apple.TextEdit
```

Every requester, agent, and application value is an exact local allowlist entry, and each option can be repeated when more than one exact identity is needed.
Wildcards are not accepted as authority.
The process opens outbound HTTPS connections to Matrix and does not listen on a network port.
Authenticated, unexpired commands received by the initial Matrix sync are dispatched during startup, including control commands when the new process has a valid local control lease.
Wait until the terminal says `Desktop bridge online` before sending new work when the caller needs confirmation that startup and device pinning completed.
The bridge durably journals an accepted command before local execution, so a Matrix redelivery after a crash returns the cached response or an unknown-outcome warning instead of repeating a started control.
Completed journal records retain bounded desktop or browser response content and screenshot decryption metadata in `<storage>/desktop_bridge/command_journal.json`, which MindRoom requires to be owner-only (`0600`) before reading.

To grant semantic and fallback control for fifteen minutes, stop the observe-only process and restart it locally with an explicit lease:

```bash
mindroom desktop run \
  --controller-user-id @computer:example.org \
  --controller-device-id CLOUDDEVICE \
  --controller-ed25519 cloud-device-fingerprint \
  --allow-requester @alice:example.org \
  --allow-agent computer \
  --allow-app com.apple.TextEdit \
  --allow-control \
  --lease-minutes 15
```

The maximum lease accepted by the CLI is sixty minutes.
The running process enforces the lease with a monotonic local deadline, so moving the wall clock backward does not extend control.
The bridge continues running after the lease expires, but every control action is rejected until a person restarts it with a new lease.

### Use the Signed-In Browser Profile

Install the official [Playwright MCP Bridge extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm) in the local browser profile.
The browser will show its normal extension installation confirmation, and MindRoom cannot bypass it.
For Brave on macOS, add these options to the local bridge command:

```bash
mindroom desktop run \
  --controller-user-id @computer:example.org \
  --controller-device-id CLOUDDEVICE \
  --controller-ed25519 cloud-device-fingerprint \
  --allow-requester @alice:example.org \
  --allow-agent computer \
  --allow-app com.brave.Browser \
  --allow-control \
  --lease-minutes 15 \
  --browser-extension \
  --browser-executable "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --browser-user-data-dir "$HOME/Library/Application Support/BraveSoftware/Brave-Browser"
```

For Chrome, omit the two explicit path options to use Playwright MCP's normal Chrome discovery, or provide the matching Chrome executable and user-data root.
An explicit `browser(action="start", target="desktop")` call starts `@playwright/mcp@0.0.78` locally through `npx` and opens the extension connection page in that profile.
Starting that connection requires the local control lease; observation calls cannot launch or foreground the browser on an observe-only bridge.
The connection page lets the user choose an initial tab and displays a reconnect token.
To reconnect after local bridge restarts without another browser prompt, store that value as `PLAYWRIGHT_MCP_EXTENSION_TOKEN` in the local MindRoom `.env` file with owner-only permissions.
Treat the reconnect token like a local browser-control credential, never commit it, and regenerate it from the extension page if it is exposed.
MindRoom passes only the safe MCP subprocess environment, this explicit reconnect token, and the selected browser profile root to the Node child, so unrelated provider API keys are not inherited.
The local bridge terminal remains the only place that can grant or renew the control lease.
Files used with `browser(action="upload", target="desktop")` must already exist under `<storage>/desktop-browser` on the local computer.

## 5. Agent Flow

The agent calls `list_apps` and selects an exact returned app ID.
If the selected app is not running, the agent calls `launch_app`, which requires the local control lease and returns fresh state when the app becomes accessible.
It then calls `get_app_state` and inspects the returned roles, names, actions, hierarchy, and screenshot.
It prefers a semantic action using an element index and the matching `state_id`.
The action response contains a new `state_id`, new element indexes, and a new screenshot for the next decision.
The agent uses normalized `click`, `type_text`, `scroll`, or `keypress` only when the accessibility state lacks the needed semantic control.
If the bridge reports stale state, the agent calls `get_app_state` again instead of reusing the old element index or coordinate.
If an action outcome is unknown or its follow-up state is incomplete, the agent observes again and does not automatically repeat the action.
Some applications change their UI successfully and then return an accessibility error, so a fresh observation is the only safe way to resolve an unknown outcome.
If the user asks to receive the screenshot, the agent calls `desktop(action="screenshot", app="...", return_attachment=true)` and then sends the returned `attachment_id` in the same turn with `matrix_message`.

For browser work, the agent first uses `browser(action="start", target="desktop")` while the local control lease is active, then calls `browser(action="tabs", target="desktop")` or `browser(action="snapshot", target="desktop")`.
The snapshot returns semantic roles, names, current values, and element references from the current page.
Reading and operating the current tab avoids treating Playwright MCP's mutable tab-list indices as durable identities.
Element references are opaque and may include frame identity, so the agent must pass each returned reference through unchanged.
The agent passes those references to `browser(action="act", target="desktop", request=...)` for form filling and interactive steps.
After navigation or a significant page update, the agent requests a new snapshot instead of reusing old references.
The `screenshot` action is useful for visual context, but semantic actions should use snapshot references rather than guessing image coordinates.
For a browser screenshot that the user should receive, the agent passes `returnAttachment=true` with `target="desktop"` and sends the returned `attachment_id` through `matrix_message` in the same turn.

## 6. Add Matrix Approval for Desktop-App Control Actions

The local lease is the hard authority boundary, while MindRoom's existing approval cards can add per-action human confirmation in the Matrix conversation.
Create `approval_scripts/desktop_control.py` beside the cloud config:

```python
CONTROL_ACTIONS = {
    "launch_app",
    "click_element",
    "set_value",
    "scroll_element",
    "perform_action",
    "click",
    "type_text",
    "scroll",
    "keypress",
}


def check(tool_name: str, arguments: dict[str, object], agent_name: str) -> bool:
    action = arguments.get("action")
    return tool_name == "desktop" and isinstance(action, str) and action in CONTROL_ACTIONS
```

Reference that script from the cloud configuration:

```yaml
tool_approval:
  default: auto_approve
  rules:
    - match: desktop
      script: ./approval_scripts/desktop_control.py
```

With this policy, observation remains immediately available while each `desktop` app-control action waits for the original Matrix requester to approve it.
This example does not cover the separate `browser` tool; add a browser-specific rule if browser calls also need Matrix approval.
Approval does not override an absent or expired local control lease.

## Operations

Rotate the local desktop Matrix device with `mindroom desktop login --replace`, revoke the old device in Matrix account management, and then update all three local device pin fields in the cloud tool configuration.
The `--replace` option creates a fresh saved session but cannot revoke the old device by itself.
If the cloud agent receives a new Matrix device, run `mindroom desktop controller --entity <name>` again and update the three controller options used locally.
A device ID or Ed25519 mismatch is a hard failure and should be treated as a rotation or possible substitution, not bypassed.
Use `Ctrl+C` to stop accepting new bridge commands and begin shutdown.
Desktop input already dispatched through a native worker thread and active Playwright MCP calls are not preemptible, so an in-flight control can still finish before shutdown returns; observe local state before deciding whether to retry it.
For stronger isolation, run the bridge in a dedicated operating-system account and expose only a non-sensitive desktop session.

## Current Limits

Native semantic accessibility is implemented only for macOS in this version.
Playwright extension mode is limited to Chromium-family browsers, so Safari and other unsupported browsers continue to use the accessibility and scoped-screenshot path.
Screenshots and pixel fallback currently target the primary display, so an app window must fit fully on that display for a scoped screenshot to succeed.
The bridge foregrounds and revalidates the allowed app before an on-screen window crop, but an always-on-top overlay inside those bounds can still appear in the screenshot.
Global keyboard shortcut chords are intentionally unavailable because they could switch to or launch an application outside the local allowlist.
The returned accessibility tree is capped and depth-bounded, and the state reports when it was truncated.
Table and outline state prefers the rows that macOS reports as visible so off-screen Finder-style content does not crowd current controls out of the bounded tree.
There is no MatrixRTC live screen stream, tray application, multi-monitor selector, unattended service installer, or remote approval of local lease changes yet.
Commands and encrypted responses are Matrix to-device messages rather than persistent room events, while normal MindRoom tool traces and optional approval cards remain visible in the Matrix conversation.
