# Matrix Desktop Bridge

The `desktop` tool lets a cloud-hosted MindRoom agent use a local computer without opening an inbound port.
It can inspect and operate explicitly allowlisted applications, read explicitly selected folders, and run shell commands that a person at the computer approves.
Applications, [read-only folders](#read-only-folders), and [shell commands](#shell-commands) are separate local choices, and each starts disabled.
The local computer runs an outbound Matrix client, and commands and responses travel end-to-end encrypted between exact paired Matrix devices.
Set up and run the bridge from the [macOS app](https://docs.mindroom.chat/installation/macos-app/#computer-access) or from the terminal; both use the same saved setup.

## Requirements

- **Matrix account**: Use your chat account or a dedicated Matrix account for the local bridge.
  The `mindroom desktop setup` command returned by `!desktop setup` adds a new device to your chat account when no local Desktop session is saved.
  For password login, a dedicated account limits the impact of exposing that password on the local computer.
  The desktop account and the cloud MindRoom agent must be able to exchange to-device events and media through the same Matrix federation.
- **Local package**: `mindroom desktop run` installs the `desktop` extra at startup when it is missing, unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
  To install it in advance, run `uv tool install 'mindroom[desktop]'` on the computer being controlled.
- **macOS applications**: Accessibility permission is required for state and control, and screenshots require macOS 14 or newer and Screen Recording permission.
  When `mindroom desktop run` starts from a terminal, macOS attributes both permissions to that terminal app and applies a new grant only after the app is quit and reopened, even if it already appears enabled; inside tmux, also restart the tmux server with `tmux kill-server`.
- **Linux applications**: Only screenshot observation of the `primary-screen` app ID is available, plus coordinate input during a control lease, on an active X11 desktop; Wayland is not supported.
- **Windows applications**: The terminal commands offer the same `primary-screen` screenshot observation and coordinate input, which have not been verified on a real Windows computer.
  On Linux and Windows, coordinate input is not bound to an app window, so whatever window covers the point receives it.
- **Read-only folders and shell commands**: These need no application selection and no Accessibility or Screen Recording permission, and they require macOS or Linux.
  On Windows, `mindroom desktop access` refuses to save them, and `mindroom desktop run` refuses to start while either is saved; run `mindroom desktop access --clear-folders --no-shell` to turn them off.
- **Signed-in browser profile** (optional): Node.js 18 or newer, Chrome or Brave, and the official Playwright MCP Bridge extension in the browser profile MindRoom will use.

A headless or locked graphical session is not a supported target.

## 1. Configure the Agent

Add the `desktop` tool to a normal or private agent:

```yaml
agents:
  computer:
    display_name: Computer Agent
    role: Operate my locally authorized applications one step at a time
    tools:
      - desktop:
          defer: true
          timeout_seconds: 30
```

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `timeout_seconds` | number | `30` | From 1 to 120; bounds each call and each shell-output download. `run_shell` always waits up to 120 seconds so the person at the computer has time to decide. |

Never put device identity fields in agent YAML for the `desktop` tool; each user pairs their own computer through chat.
Pairings are scoped to one requester and one agent, so add `private: {per: user_agent}` only when the agent's whole runtime and state should also be requester-private.
The tool needs a live Matrix room and runs in the primary agent process, so it is unavailable in [OpenAI-compatible API](https://docs.mindroom.chat/openai-api/) runs.
To drive the user's signed-in browser, configure the separate [`browser`](https://docs.mindroom.chat/tools/web-scraping-and-browser/#browser) tool with the bridge's device identity, then follow [Use the Signed-In Browser Profile](#use-the-signed-in-browser-profile).

## 2. Pair the Computer

1. In a private Matrix room containing only you and one Desktop-enabled agent, plus the router when it serves the command, send `!desktop setup`.
   The reply contains setup data for the macOS app and an equivalent `mindroom desktop setup` command, which name the homeserver you sign in to, your Matrix account, the agent, the cloud agent's exact device identity, and a short-lived pairing code.
   The pairing code is a bearer secret, so MindRoom refuses `!desktop` when any other member is present or when it cannot confirm the room's complete membership, including pending invites.
   If the router could not look up the membership, it asks you to try again in a moment.
2. In the macOS app, paste the reply's JSON setup data into **Computer access** and follow the [Computer Access steps](https://docs.mindroom.chat/installation/macos-app/#computer-access).
   In a terminal, run the full `mindroom desktop setup` command from the reply once; it reuses a saved Desktop Matrix session or logs in first, then claims the pairing.
   Add `--allow-app com.apple.TextEdit` to save an app choice at the same time.
3. Copy the printed `!desktop confirm <code> <verification>` command back into the same chat.
   The verification value ties confirmation to the device that claimed the code, so another device cannot take over a visible pairing code.

Only the same requester with the same agent can confirm or use the pairing, and shared credentials are never used as a fallback.
Until pairing is confirmed, `desktop` calls return a setup-required result while the agent's other tools keep working, and the model cannot register or replace a device itself.
Use `!desktop status`, `!desktop rotate`, and `!desktop disconnect` as described in [`!desktop`](https://docs.mindroom.chat/chat-commands/#desktop).

Repeating setup for the same controller keeps saved browser, capture, folder, and shell choices, and keeps app choices unless you supply new app IDs.
Setup for a different controller starts without saved folders or shell access.
If the saved session belongs to a different homeserver or Matrix user than the setup command names, setup exits without pairing; pass `--storage-path` for a separate setup or run `mindroom desktop login --replace`.
The separate `mindroom desktop login` and `mindroom desktop pair` commands remain available for manual recovery.

### Log In Manually

`mindroom desktop login` creates the local Desktop Matrix device.
By default it checks the homeserver's advertised methods, opens Matrix SSO in the browser when available, and otherwise uses password login; `--login-method password` or `--login-method sso` selects one explicitly.

```bash
# SSO
mindroom desktop login --homeserver https://matrix.example.org

# Password login for a dedicated account such as @my-laptop:example.org
mindroom desktop login \
  --user-id @my-laptop:example.org \
  --homeserver https://matrix.example.org \
  --login-method password
```

Password login requires `--user-id` and prompts for the password with hidden input.
During SSO, `--user-id` is an optional identity check, and MindRoom rejects the new session if SSO returns a different Matrix user.
Use `--sso-idp ID` to pick one of several SSO providers, and `--no-open-browser` to print the URL instead of opening it.
MindRoom does not create cross-signing keys for the device; the bridge trusts the exact device ID and Ed25519 key shown after login instead.
The command prints values similar to these:

```text
User: @my-laptop:example.org
Device: ABCDEFGHIJ
Ed25519: desktop-device-fingerprint
```

All `mindroom desktop` commands use the `~/.mindroom` runtime regardless of the current directory.
An explicit `--config`, `--storage-path`, `MINDROOM_CONFIG_PATH`, or `MINDROOM_STORAGE_PATH` selects another runtime and must be reused for login, setup, pairing, and `mindroom desktop run`.
On Unix, the saved session file must be owner-only (`0600`), and the bridge refuses to load it if group or other users can read it.
Run `mindroom desktop login --replace` to replace a saved session with a fresh device.

### Homeservers Behind an Identity-Aware Proxy

For a homeserver protected by Cloudflare Access, install the [`cloudflared` CLI](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) and add `--cloudflare-access` to `mindroom desktop login` or `mindroom desktop setup`:

```bash
mindroom desktop login \
  --homeserver https://matrix.example.org \
  --cloudflare-access
```

`cloudflared` opens the browser whenever Access needs authentication, and MindRoom never saves the Access token.
The session remembers the choice, so later `mindroom desktop run` commands and the macOS app need no flag.
For a session saved without it, `mindroom desktop run --cloudflare-access` enables Access for that run only.

Two environment variables on the MindRoom server change the commands that `!desktop setup` prints:

- `MINDROOM_DESKTOP_MATRIX_HOMESERVER`: the public Matrix URL local Desktop clients should use when the server reaches Matrix through an internal address; server traffic keeps using `MATRIX_HOMESERVER`.
- `MINDROOM_DESKTOP_CLOUDFLARE_ACCESS=true`: add `--cloudflare-access` to the printed login and pairing commands.

For unattended proxies that issue machine credentials instead, put the exact request headers the proxy requires in a JSON object of string keys and values:

```json
{
  "X-Access-Client-Id": "client-id",
  "X-Access-Client-Secret": "client-secret"
}
```

Restrict the file to its owner and pass it to both login and the bridge, or set `MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE` to its path:

```bash
chmod 600 ~/.config/mindroom/matrix-http-headers.json

mindroom desktop login \
  --user-id @my-laptop:example.org \
  --homeserver https://matrix.example.org \
  --matrix-http-headers-file ~/.config/mindroom/matrix-http-headers.json
```

MindRoom refuses group-readable or world-readable header files on Unix, adds the headers to every Matrix request the desktop client makes, and does not copy them into the saved session.
During SSO, the browser handles interactive proxy authentication while the header file authenticates the command-line requests.
Accept these machine credentials only for the Matrix endpoints the desktop device needs, keep normal Matrix authentication enabled, and do not combine `--cloudflare-access` with a static `cf-access-token` header.

## 3. Choose Local Access

Applications, read-only folders, and shell commands are saved separately, and the bridge exposes only what is saved.

### Choose Applications

Save applications in the macOS app's **Access** step or with repeated `--allow-app` on `mindroom desktop setup`; `--allow-app` on `mindroom desktop run` applies to that run only.
On macOS, use exact bundle identifiers such as `com.apple.TextEdit` or `com.brave.Browser`; you can look one up without granting access:

```bash
mdls -name kMDItemCFBundleIdentifier /System/Applications/TextEdit.app
```

Add only the applications needed for the current task.
The special app ID `primary-screen` exposes the whole primary display without semantic elements; on Linux and Windows it is the only usable target.
Controlling `primary-screen`, a terminal emulator, a scripting or automation app, or a browser or Matrix client signed in as you hands the agent your own authority, as the [Security Model](#security-model) warning explains.

### Read-Only Folders and Shell Commands from the Terminal

Choose folders and shell access in the macOS app's **Access** step, or save them from the terminal for the same connection:

```bash
mindroom desktop access --allow-folder ~/Documents/project-notes --shell
```

Repeat `--allow-folder` to add more existing folders, use `--clear-folders` to remove every saved folder, and use `--no-shell` to turn shell requests off.
Omitted options keep the saved values, and the command prints the saved applications, folders, and shell setting.
It requires completed setup and never starts the bridge or grants auto-approval.

## 4. Run the Local Bridge

Start the bridge from the macOS app or with:

```bash
mindroom desktop run
```

The command uses the same saved controller, allowlists, folders, shell setting, browser settings, and capture settings as the macOS app.
Stop the bridge in the interface that started it before switching interfaces.
Saved changes take effect on the next start and never change a running bridge's authority.
The bridge opens only outbound HTTPS connections to Matrix and does not listen on a network port.
Wait for `Desktop bridge online` before sending work when you need confirmation that startup completed.

Flags override saved settings for one run without changing them; [`mindroom desktop run`](https://docs.mindroom.chat/cli/#desktop-run) lists every option.
`--allow-requester`, `--allow-agent`, and `--allow-app` take exact Matrix user IDs, agent names, and app IDs, each option can be repeated, and wildcards are not accepted.
Changing the controller requires `--controller-user-id`, `--controller-device-id`, `--controller-ed25519`, and all three allowlist options, and such a run starts without saved folders or shell access.
The `!desktop confirm` reply prints a `mindroom desktop run` command with these values filled in except the app ID.

### Grant Application Control

Applications start observe-only.
To allow semantic and coordinate input, a person at the computer grants a control lease with **Grant Control…** in the macOS app or by restarting the terminal bridge with `--allow-control`:

```bash
mindroom desktop run --allow-control --lease-minutes 15
```

`--lease-minutes` defaults to 15 and accepts at most 60, and changing the wall clock does not extend a lease.
After the lease expires, the bridge keeps observing but rejects every control action until a person grants another lease.
The macOS app can grant or revoke without restarting; the terminal requires a new invocation.
Leases are never saved or renewed automatically, so a restarted bridge is observe-only again.

### Approve Shell Commands in the Terminal

With shell requests saved, each command appears in the terminal running `mindroom desktop run` with its ID, expiry, requester, agent, working directory, and command, and waits for one answer:

```text
[a]pprove once, [r]eject, [5]/[15]/[60] minutes, [u]ntil stopped:
```

Only an answer typed after the prompt counts, and the command's length appears right above the prompt, so scroll up when the request is longer than the terminal.
If standard input is not an interactive terminal, input reaches its end, or the bridge runs as a background job, requests are rejected with guidance instead of waiting.
Start with `--shell-auto-approve-minutes`, from 1 to 60, to approve commands without asking for part of the run; this requires saved shell access and is never saved.
The terminal has no separate revoke command; press `Ctrl+C` to stop the bridge instead.
See [Local Approval](#local-approval) for what each choice covers.

### Use the Signed-In Browser Profile

The `browser` tool's desktop target drives the user's real browser profile through the official [Playwright MCP Bridge extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm).
Install the extension in the local browser profile, then start the bridge with `--browser-extension`.
For Brave on macOS, also pass its executable and user-data root:

```bash
mindroom desktop run \
  --allow-control \
  --browser-extension \
  --browser-executable "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --browser-user-data-dir "$HOME/Library/Application Support/BraveSoftware/Brave-Browser"
```

For Chrome, omit the two path options to use normal Chrome discovery, or provide the matching Chrome paths.
A `browser_control(action="start", target="desktop")` call requires the local control lease, starts `@playwright/mcp@0.0.78` locally through `npx`, and opens the extension's connection page, where the user chooses an initial tab.
To reconnect after bridge restarts without another browser prompt, store the reconnect token that page shows as `PLAYWRIGHT_MCP_EXTENSION_TOKEN` in the local MindRoom `.env` file with owner-only permissions.
Treat that token as a browser-control credential, never commit it, and regenerate it from the extension page if it is exposed.
The local browser process does not receive unrelated provider API keys.

The desktop target can observe tabs, page snapshots, screenshots, and the console, and can start, stop, and open a new tab; it cannot act on, navigate, or close an existing page.
See [`browser`](https://docs.mindroom.chat/tools/web-scraping-and-browser/#browser) for its configuration and actions.
For app-level control of the browser window, or for Safari and other browsers, allowlist the browser as an application instead.

## Security Model

- **Control is local**: Applications are observe-only until a person at the computer grants a control lease, and only the macOS app's **Computer access** section or the local terminal can grant or renew it.
- **Exact identities**: The bridge accepts commands only from the paired cloud agent device, for locally allowed requesters, agents, and apps, and rejects expired or replayed commands.
- **Local allowlists**: Only applications selected locally can be listed, launched, inspected, captured, or controlled.
  An allowed app can still open a link or document in another app, which the bridge cannot then inspect or control unless that app is also allowlisted.
  Allowlisting a browser exposes whichever window and tab is selected, not one website.
- **MindRoom is excluded**: MindRoom itself (`chat.mindroom.menubar`) and its desktop helper (`chat.mindroom.desktophelper`) can never be allowlisted, because their windows grant shell auto-approval, control leases, and app access.
- **No remote authority**: Cloud configuration, chat messages, and model output cannot enable control, extend a lease, change local allowlists or folders, enable shell commands, or approve one.
- **Emergency stop**: Moving the pointer to the upper-left corner of the primary display stops control and keeps it off until the bridge restarts or a person selects **Reset Emergency Stop** in the macOS app while no action is running; the reset does not grant control.
- **Local audit log**: The bridge logs each completed or rejected command without its parameters, values, or typed text.
- **One bridge per session**: While a shell command waits for local approval, this bridge refuses every control action except `launch_app`, so its own agents cannot click or type into the approval.
  Another bridge's agents could, so run at most one bridge in each logged-in graphical session.
- **Not exposed**: The bridge offers no clipboard, microphone, webcam, unlock, privilege elevation, or arbitrary local RPC, and it cannot bypass operating-system permission prompts.

!!! warning "Some allowlisted apps give an agent your own authority"
    Controlling a terminal emulator, a scripting or automation app such as Script Editor, Shortcuts, or Automator, or the `primary-screen` target lets an agent run anything your account can, without any shell approval.
    The agent can type commands into a terminal or script, and `primary-screen` input reaches whatever app is under the pointer or has focus, including MindRoom's own windows while no shell request is pending.
    Controlling a web browser or Matrix client signed in as you, such as MindRoom Chat in a browser or Element, or a browser with a local MindRoom dashboard session, lets an agent act as you: it can answer its own Matrix tool approvals, send `!` commands, and instruct other agents.
    Allowlist these only when you would also approve everything the agent could run or send as you.

Enabling the browser extension grants access to every tab and the signed-in state of the connected profile, not one tab or origin.
It is not a network sandbox: MindRoom validates URLs passed to `open`, but redirects and page scripts keep the profile's normal network reach.

### What Leaves the Computer

The Matrix homeserver sees routing metadata, timing, and encrypted media size, but not commands, screenshots, file contents, or output.
After MindRoom decrypts them in the cloud, accessibility state, screenshots, file contents, and command output become model input, so your model provider receives them.
Accessibility data can include labels, document text, and form values not obvious from the screenshot; macOS secure text fields are suppressed and cannot be changed semantically.
Model-visible screenshots and other results can remain in agent session storage, tool traces, and automatically saved workspace files, so protect and expire that storage according to the sensitivity of the controlled applications.
Action arguments, shell commands, and paths appear in model context, approval cards, and Matrix tool traces, so never use `set_value`, `type_text`, or `run_shell` with passwords, tokens, recovery codes, or other secrets.
Screenshots, labels, web content, file contents, and command output are untrusted data and are never user authorization or instructions.
The bridge keeps command arguments and outcomes, including shell commands, file text, and inline shell output, in `<storage>/desktop_bridge/commands.sqlite3`, which must be owner-only (`0600`); it stores no plaintext screenshots.

## Applications

Applications are accessibility-first on macOS.
`get_app_state` returns a bounded accessibility tree with roles, names, values, bounds, writable state, and advertised actions, plus a screenshot of the selected app window.
The agent normally acts on an `element_ref` from that state instead of guessing a coordinate, and every completed action returns fresh state and a fresh screenshot for the next step.
Taking a screenshot on macOS brings the selected app to the front.

Observation actions:

- `status` reports screen, cursor, accessibility backend, lease state, which capabilities are enabled, whether a shell command is waiting, remaining auto-approval time, and the caller's own shell handles.
  Its `bridge.gui_mode` (`observe_only` or `control`) describes only application control.
- `request_status` reads a retained outcome without executing it again; see [Uncertain Outcomes](#uncertain-outcomes).
- `list_apps` lists locally allowlisted app IDs and whether each is running.
- `get_app_state` returns fresh state and screenshot.
- `screenshot` returns fresh state and requires the screenshot to succeed.
  With `return_attachment=true`, it also returns a turn-scoped `att_*` handle that `matrix_message` can send in the same turn without saving or uploading the image again.

Control actions, which require a control lease:

- `launch_app` launches or foregrounds one allowlisted app.
- `click_element`, `perform_action`, and `scroll_element` press, invoke an advertised action on, or scroll the selected element.
- `set_value` changes an accessibility value only when the element reports it is writable.
- `click`, `double_click`, and `hover` use a coordinate from `0` to `1000` inside the app window, not raw screen pixels.
- `drag` holds the left button between two such points over 100 to 2,000 milliseconds.
- `type_text` types up to 2,000 characters into the focused app, or into an exact `element_ref` or `element_index` that must have focus.
  Without an element, text cannot contain line breaks, tabs, or other control characters, so send Enter or Tab with `keypress`.
- `scroll` scrolls a bounded number of pages in one direction at the app or an optional app coordinate.
- `keypress` supports navigation keys, shift plus navigation, command or ctrl plus a, c, x, v, z, or f, and command or ctrl plus shift plus z.
  App-switching, quit, launch, and address-bar shortcuts are rejected because they could reach apps outside the allowlist.

Pass `observation="tree"` to skip screenshot capture, upload, and image input when semantic state is enough, or `observation="screenshot"` to omit only the element list; the default is `both`, and the `screenshot` action rejects `tree`.

Element references expire after 120 seconds or when the app's process, window, or target element changes, and a newer observation does not invalidate an unchanged older target.
When the bridge reports stale state, call `get_app_state` again instead of reusing the old reference or coordinate.
A large tree is capped and returns `truncated: true`, and a tree still changing returns `stability: unstable`.
Coordinate input, unscoped typing, `scroll`, and `keypress` need a fresh, complete, stable observation, so let the UI settle and observe again first.
On macOS, keyboard input fails without being sent if the app has lost focus, and pointer input fails if another app's window, such as a notification banner, floating panel, or Picture in Picture, covers the target point; if either happens after input started, input stops and the outcome is unknown.
Windows on secondary displays are supported when the window lies on one display; move a window that spans displays fully onto one.

## Read-Only Folders

The agent can list and read inside locally selected folders, and there is no write, create, rename, or delete action.

- `list_folders` returns an ID, name, and absolute path for each selected folder.
- `list_directory` takes a `root_id` and optional relative `path` and returns at most 200 entries with their type (`directory`, `file`, `symlink`, or `other`) and a `truncated` flag.
- `read_file` takes a `root_id`, a relative `path`, and an optional byte `offset`, and returns at most 16 KiB of UTF-8 text with `next_offset`, `eof`, and `truncated`; continue a longer file from `next_offset`.

Paths are relative to the selected folder, absolute paths and `..` components are rejected, and symbolic links are never followed, so a link cannot reach a file outside the folder.
Only regular text files are read; FIFOs, devices, files with control bytes, and invalid UTF-8 are rejected.
Local file permissions still apply, and macOS may still ask before MindRoom reads protected folders such as Desktop, Documents, or Downloads; MindRoom never requests Full Disk Access.
A saved folder that no longer exists makes startup fail with `Cannot open local folder`.

## Shell Commands

!!! warning "Shell commands run with your full account access"
    An approved command runs as your user account, with the network and every file that account can read or change, including files outside the selected folders.
    Neither the selected folders nor the working directory confine it, and it is not a sandbox.
    Approve only commands you would run yourself, and run the bridge in a dedicated operating-system account when you need stronger isolation.

Enable shell requests locally with **Allow shell command requests** in the macOS app's **Access** step or with `mindroom desktop access --shell`.

- `run_shell` runs `command`, up to 8,192 characters, with `/bin/sh -c` in `cwd`, an absolute local directory that defaults to the home directory.
  `timeout_seconds`, from 1 to 60 with a default of 30, is how long the call waits for the command to finish before returning a handle.
- `check_shell` returns a handle's newest output while it runs, and its complete output and exit code once it finishes; pass `offset` to read from a byte position.
- `kill_shell` asks a handle's command to terminate, or kills it immediately with `force=true`.
  A later `check_shell` reports `state: "killed"`, while `completed` means the command exited on its own.

A `command` or `cwd` with more than 64 whitespace or invisible characters in a row, more than two blank lines in a row, or more than 8 combining marks on one character is refused before approval, because such padding could hide part of the request from the approver.

### Local Approval

Every `run_shell` request waits for a decision from the person at the computer, on the macOS app's approval card or at the `mindroom desktop run` terminal prompt, unless auto-approval is active.
The approver sees the exact command, working directory, requester, agent, expiry, and command length, with invisible and control characters escaped and a warning when a field contains look-alike non-ASCII characters.
The choices are reject, approve once, or approve and also auto-approve later commands for 5, 15, or 60 minutes or until shell access is revoked or the bridge stops.
There is no remote approval, so a chat message, the agent, or cloud configuration cannot approve a command or grant auto-approval.
A request nobody answers within 120 seconds expires, and rejected, expired, or revoked requests never run.

!!! warning "Auto-approval covers every allowed caller"
    Timed and until-stopped auto-approval approves every shell command from all locally allowed requesters and agents, not only the command that prompted it.
    Auto-approval is never saved: it ends when its time runs out, when you revoke shell access, or when the bridge stops, and a restarted bridge asks for each command again.

In the macOS app, **Allow Without Asking…** grants auto-approval before any request arrives; in the terminal, use `--shell-auto-approve-minutes`.
Besides ending auto-approval, revoking shell access or stopping the bridge rejects a waiting request and kills every running command.

### Handles

A command still running after its inline wait keeps running as a handle instead of being killed.
The reply has `state: "running"`, the handle, and the newest output so far; the agent polls with `check_shell` and stops it with `kill_shell`.
Results report `output_start` and `next_offset` for the returned byte range and `output_bytes` for the total captured; pass the last `next_offset` to `check_shell` to read only newer output, and an `output_start` above the requested offset means earlier output was skipped.
Handles belong to the requester and agent that started them, and checking or killing your own handle needs no approval.
The macOS app lists every handle with its requester, agent, command preview, elapsed time, and state, and can kill running ones.
A `check_shell` that returns a finished handle's remaining output in full forgets the handle.

The bridge keeps at most 16 handles.
When all 16 are retained, a new command drops the handle that finished longest ago, with its output, and a new command is refused if all 16 are still running.
Each handle keeps up to 10 MiB of output in private temporary files until it is read in full, dropped for a new command, or dropped when a new command starts more than 10 minutes after it finished.
Handles do not survive revoking shell access or a bridge restart, so a later `check_shell` reports an unknown handle.
When a command finishes, other processes in its process group, such as children started with `&`, are terminated, so keep long-running work in the foreground and let it continue as a handle.

### Environment and Output

Commands run with your login shell's environment, captured once when the bridge starts, so profile changes apply at the next start.
If capture fails or takes more than 5 seconds, commands get a minimal environment of `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`, and a `PATH` that includes the Homebrew and `~/.local/bin` directories.
The bridge's own credentials are never passed to commands.

!!! warning "Your shell profile's exports are visible to commands"
    Anything your shell profile exports, including tokens and other secrets, is visible to every approved command.
    Commands could already read the profile files that set those values, but a command that prints one sends it to the model provider like any other output.

Each call runs in a fresh non-interactive `/bin/sh`, so directory changes and variables never carry over, and `$SHELL` is still your login shell.
Commands have no terminal and read standard input from `/dev/null`, so they cannot answer password prompts.
Standard error is merged into standard output; redirect it with `2>file` to keep it separate.

The bridge captures up to 10 MiB of output per command and reports `output_truncated: true` when more was dropped.
Larger output arrives as an encrypted Matrix attachment that the cloud tool downloads within its `timeout_seconds`.
If that delivery fails, the reply shows the output that fits with a `warning`, and `check_shell` from `next_offset` reads the rest.
For agents with a workspace, MindRoom's normal [tool output files](https://docs.mindroom.chat/tools/execution-and-coding/#common-setup-notes) apply: a result larger than the automatic-save threshold, 50 KiB by default, is saved under `mindroom_tool_outputs/` in the agent workspace and returned as a preview, and `mindroom_output_path` chooses the file.

## Uncertain Outcomes

A timeout returns the original `request_id` and an unknown outcome; the action may still be queued, may have run, or, for `run_shell`, may still be waiting for approval or running.
Call `desktop(action="request_status", request_id="...")` with a new request to read `queued`, `running`, `completed`, `unknown`, or `not_found`, including after the bridge restarts.
Only the requester and agent that sent the action can read its outcome, from any conversation; other callers get `not_found`.
Request IDs must be unique across actions and queries, and `not_found` is not evidence that the action never ran.
Do not repeat an uncertain action automatically; observe current state first.
Some apps change their UI and then report an accessibility error, so a fresh observation is the only reliable way to resolve an unknown app action.
Interrupted work is never run again and reports an unknown outcome.
Never resubmit or rephrase a rejected or expired shell command.

## Require Matrix Approval for App Control

The local lease is the hard authority boundary, but MindRoom's [tool approval](https://docs.mindroom.chat/configuration/#tool-approval) cards can add per-action confirmation in the Matrix conversation.
Create `approval_scripts/desktop_control.py` beside the cloud config:

```python
CONTROL_ACTIONS = {
    "launch_app",
    "click_element",
    "set_value",
    "scroll_element",
    "perform_action",
    "click",
    "double_click",
    "hover",
    "drag",
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

Observation stays immediate while each app-control action waits for the original requester's approval in Matrix.
Matrix approval does not replace an absent or expired local lease, and it does not cover the separate `browser` tool.
Adding `run_shell` to the set adds a chat confirmation before the request is sent, and the command still waits for approval on the computer.

## Operations

- **Rotate the local device**: Run `mindroom desktop login --replace`, revoke the old device in your Matrix account settings, then run `!desktop rotate` in the agent chat and follow its pairing command.
  `--replace` does not revoke the old device by itself.
- **Change the cloud controller**: Once the bridge has a command history, saving a different controller identity is rejected, because pending work stays bound to its original controller.
  Use `--storage-path` with a new directory for `mindroom desktop login`, `mindroom desktop setup`, and `mindroom desktop run`; the new setup does not carry over pending commands or outcomes, and the macOS app has no migration action.
- **Identity mismatch**: A device ID or Ed25519 mismatch is a hard failure; treat it as a rotation or possible substitution and do not bypass it.
- **Stop**: `Ctrl+C` in the terminal or **Stop Access** in the macOS app stops the bridge; see [Local Approval](#local-approval) for what that does to shell commands.
  An app or browser action already in progress can still finish, so observe local state before retrying it.
- **Stronger isolation**: Run the bridge in a dedicated operating-system account and expose only a non-sensitive desktop session.

## Current Limits

There is no live screen stream, multi-monitor selector, or unattended service installer.
