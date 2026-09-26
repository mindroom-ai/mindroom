---
icon: lucide/monitor-up
---

# Matrix Desktop Bridge

The `desktop` tool lets a cloud-hosted MindRoom agent use a local computer without opening an inbound port.
It can inspect and operate explicitly allowlisted applications, read explicitly selected folders, and run shell commands that a person at the computer approves.
Applications, [read-only folders](#read-only-folders), and [shell commands](#shell-commands) are separate local choices, and each starts disabled.
The local computer runs an outbound Matrix sync client, while commands and responses use Olm-encrypted to-device events addressed to exact pinned Matrix devices.
App screenshots are encrypted before upload to Matrix media, and their decryption keys travel only inside the encrypted response.
By default, the screenshot is model-visible and MindRoom does not create a separate plaintext attachment file on the cloud host.
Agno's normal agent-session persistence can retain model-visible screenshot pixels in the session database, so protect and expire that storage according to the sensitivity of the controlled applications.
When a user asks to receive the image, `desktop(action="screenshot", return_attachment=true)` returns a turn-scoped `att_*` handle that can be sent with `matrix_message`.
That handle reuses the existing encrypted Matrix media instead of saving or uploading the screenshot again, and it expires when the turn ends.

The bridge is accessibility-first on macOS.
It returns a bounded accessibility tree with roles, names, values, bounds, writable state, and advertised actions, plus a screenshot cropped to the selected app window.
The agent normally selects an opaque `element_ref` from that state instead of guessing a screen coordinate.
The bridge retains up to 128 observations for 120 seconds, bound to the requester, agent, command session, and exact app.
An `element_index` remains supported within its matching `state_id`.
A new observation does not invalidate an older unchanged target; expiration, process/window changes, and changed target identity still reject it.
The local bridge pins the exact local process, window, element, and ancestor identity and rechecks them before acting.
Passive labels such as clocks may change without invalidating an unchanged actionable target.
Full actionable text and identity stay checked even when returned text is truncated.
On macOS, complete trees undergo bounded stabilization sampling; a changing tree can still return a `state_id` with `stability: unstable`.
A capped partial tree returns immediately with `truncated: true` and `stability: truncated`.

Pixel and keyboard operations remain explicit fallbacks for controls that do not expose useful accessibility elements.
Fallback coordinates run from `0` to `1000` inside the selected app window rather than using raw desktop pixels.
Every completed action normally returns a new accessibility state and a new app-window screenshot for the next step.
If follow-up state or capture fails after an action completes, the tool tells the agent not to repeat the action automatically.
On macOS, taking the scoped screenshot foregrounds the selected application, so observation can change which local window is active.

## Observations and recovery

Pass `observation="tree"` to native app actions when semantic state is sufficient.
Tree mode skips screenshot capture, encrypted media upload, and image input entirely.
`observation="screenshot"` omits the element list while retaining app/state/coordinate metadata; `both` remains the default.
The explicit `screenshot` action requires pixels.
Results include elapsed execution time and structured/image byte counts.

A timeout returns the original `request_id` and an unknown outcome.
Call `desktop(action="request_status", request_id="...")` with a new query request to read `queued`, `running`, `completed`, `unknown`, or `not_found`.
The query can recover the same requester's and agent's receipt across command sessions, including after local restart.
It does not wait behind the action executor or evict the receipt it reads.
Request IDs must be unique across actions and queries.
A missing receipt is not evidence that the action never ran.
Do not repeat an uncertain action automatically; inspect current state first.

The local SQLite journal commits command admission before Matrix acknowledgement and records an execution start before input.
After a crash, queued work is rechecked against current trust, deadline, policy, and lease.
Started work with no outcome becomes unknown; it is never executed again automatically.
Responses retry independently without repeating the original action.
The private journal contains command arguments and structured outcomes, including shell commands, file text, inline shell output, and encrypted media descriptors; it does not store plaintext screenshot files.
An existing legacy replay journal is imported without resending historical outcomes.

On macOS, captures return logical bounds, display identity and scale, source pixel dimensions, and output image dimensions.
Secondary displays and negative logical origins are supported when the window fits one unambiguous display.
Move a window fully onto one display if it spans displays or its mapping cannot be verified.

## Security Model

For applications, the local bridge starts in observe-only mode unless a person at the computer grants a short control lease in Desktop Control or on the command line.
The local process independently checks the exact cloud Matrix user, device ID, Ed25519 fingerprint, human requester ID, agent name, app ID, command expiry, request ID, and monotonic session sequence.
Only applications selected locally in Desktop Control or named with `--allow-app` can be listed, launched, inspected, captured, or controlled.
Cloud configuration and model output cannot add an application to that allowlist.
The allowlist restricts the bridge's direct target, but an allowed app can still cause operating-system side effects such as opening a link or document in another app.
The bridge cannot then inspect or control that newly opened app unless its exact app ID is also locally allowlisted.
The local bridge executes actions serially and revalidates each target immediately before input.
Matrix polling, durable admission, and response delivery continue independently during slow actions.
Cloud configuration cannot enable control, extend a running lease, change any local allowlist or selected folder, enable shell commands, or approve one.
Restarting the local bridge returns it to observe-only mode unless `--allow-control` is supplied again.
Moving the pointer to the upper-left corner of the primary display triggers the emergency stop and latches control off.
Desktop Control can reset the latch while idle and away from that corner; resetting does not grant control.
The local process writes an audit log entry for each completed or rejected parsed command without logging action parameters, values, or typed text.

The observation actions are:

- `status` reports coarse screen, cursor, accessibility-backend, and local lease state.
- `request_status` reads a retained request outcome for the original requester and agent without executing it again.
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
- `click` and `double_click` click a normalized fallback coordinate inside the app window.
- `hover` moves the pointer within the validated app window.
- `drag` moves with the left button held between two normalized points in the same window, over 100–2,000 milliseconds.
- `type_text` types up to 2,000 characters into the validated and focused app; an optional `element_ref` or `element_index` requires that exact element to have focus.
- `scroll` scrolls a bounded number of pages up, down, left, or right at the app or an optional normalized app coordinate.
- `keypress` supports navigation keys, shift plus navigation, command/ctrl plus a/c/x/v/z/f, and command/ctrl plus shift plus z.
  Global switching, quit, launch, and address-bar shortcuts are rejected.

Folder reads and shell commands are separate opt-in capabilities with their own limits, described in [Read-Only Folders](#read-only-folders) and [Shell Commands](#shell-commands).
The bridge does not expose a clipboard, microphone, webcam, unlock operation, privilege elevation, or arbitrary local RPC.
App control operates only the currently logged-in graphical session, and no bridge capability can bypass operating-system permission prompts.

The optional Playwright MCP extension path is a separate browser capability on the same pinned Matrix transport and local control lease.
It returns semantic page snapshots and stable element references from the browser, which are usually more precise for forms and interactive websites than desktop coordinates.
Its observation actions are `status`, `profiles`, `tabs`, `snapshot`, `screenshot`, and `console`.
Its supported control actions are `start`, `stop`, and `open` a new tab.
Existing-page mutations, including `act`, `navigate`, `close`, `focus`, `pdf`, `upload`, and `dialog`, fail before MCP dispatch because this pinned backend does not expose stable page identity.
The provider reports `stable_targeting: false` and `supported_control_actions` explicitly.
Enabling the extension grants access to the tabs and signed-in state in the connected browser profile, and the desktop application allowlist does not narrow that access to one tab or origin.
Extension mode is not a network sandbox: MindRoom validates URLs passed to `open`, while redirects and page scripts retain the connected profile's normal network reach.
The local MCP process uses the pinned package version documented below.
Browser observations describe the current page.
A closed or crashed tab can cause upstream MCP to select another page automatically.
A fresh snapshot, reconnect, URL/title match, or numeric index cannot make subsequent existing-page mutation safe with this backend.
The browser extension and MCP process communicate over a machine-local loopback connection, while every cloud-to-local command still travels through pinned Matrix Olm encryption.

Matrix protects the local-to-cloud transport, but accessibility state and screenshots become model input after MindRoom decrypts them in the cloud process.
Accessibility APIs can expose labels, document text, form values, and other semantic content that is not obvious from the screenshot alone.
The macOS backend recognizes secure text fields from both accessibility roles and subroles, suppresses their values, and refuses to change them semantically.
Your configured model provider can therefore receive both the returned accessibility fields and visible app contents.
File contents and shell command output likewise become model input, so the model provider receives them too.
Desktop action arguments can also appear in model context, approval cards, and MindRoom tool traces, so never use `set_value` or `type_text` for passwords, tokens, recovery codes, or other secrets.
App screenshots, labels, document values, web content, file contents, and command output are untrusted data and must never be treated as user authorization or instructions.
Allowlisting a browser grants the bridge access to whichever browser window and tab is selected, not to one origin or website.
The Matrix homeserver can observe routing metadata, timing, and encrypted media size, but not the command body, screenshot, file, or output plaintext.

## Read-Only Folders

Select folders locally in the macOS app's **Access** step or with [`mindroom desktop access --allow-folder`](#read-only-folders-and-shell-commands-from-the-terminal); cloud configuration and model output cannot add one.
The agent can list and read inside those folders, and there is no write, create, rename, or delete action.

- `list_folders` returns an ID, name, and absolute path for each selected folder, plus a `truncated` flag if too many folders or paths are too long to fit one reply.
- `list_directory` takes a `root_id` and an optional relative `path` and returns at most 200 entries, each with its name and type (`directory`, `file`, `symlink`, or `other`), plus a `truncated` flag if there are more entries than fit the folder limit or one reply.
- `read_file` takes a `root_id`, a relative `path`, and an optional byte `offset` and returns at most 16 KiB of UTF-8 text with `next_offset`, `eof`, and `truncated`; the agent continues a longer file from `next_offset`.

Paths are relative to the selected folder, and absolute paths or `..` components are rejected.
The bridge opens every path component relative to a folder descriptor pinned at startup and never follows symbolic links, so a link inside a selected folder cannot reach a file outside it.
Only regular text files are read; FIFOs, devices, files with control bytes, and invalid UTF-8 are rejected.
Local file permissions still apply.
macOS may still ask before MindRoom reads protected folders such as Desktop, Documents, or Downloads; saving a folder does not grant that access, and MindRoom never requests Full Disk Access.
Folders are pinned when the bridge starts, so saved changes apply at the next start, and a saved folder that no longer exists makes startup fail with `Cannot open local folder`.

## Shell Commands

!!! warning "Shell commands run with your full account access"
    An approved command runs as your user account, with the network and every file that account can read or change, including files outside the selected folders.
    Neither the selected folders nor the working directory confine it, and it is not a sandbox.
    Approve only commands you would run yourself, and run the bridge in a dedicated operating-system account when you need stronger isolation.

Shell requests are off by default.
Enable them locally with **Allow shell command requests** in the macOS app's **Access** step or with `mindroom desktop access --shell`; cloud configuration and model output cannot enable them.

- `run_shell` runs `command`, up to 8,192 characters, with `/bin/sh -c` in `cwd`, an absolute local directory that defaults to the local home directory.
  Its `timeout_seconds`, from 1 to 60 with a default of 30, is how long the call waits for the command to finish before returning a handle.
- `check_shell` returns a handle's newest output while it runs, and its complete output and exit code once it has finished.
- `kill_shell` asks a handle's command to terminate, or kills it immediately with `force=true`.

### Local Approval

Every `run_shell` request waits for a decision from the person at the computer, either on the approval card in the macOS app or at the prompt in the terminal running `mindroom desktop run`, unless an earlier choice already auto-approved it.
The approver sees the exact command, working directory, requester, agent, and expiry, with control and text-direction characters shown escaped.
The choices are reject, approve once, or approve and also auto-approve later commands for 5, 15, or 60 minutes or until shell access is revoked or the bridge stops.
There is no remote approval operation, so a chat message, the agent, or cloud configuration cannot approve a command, extend auto-approval, or grant it.
The cloud call waits up to 120 seconds, the command's lifetime, and a request that nobody approves in time expires without running.
Rejection, expiry, revocation, and bridge stop before approval never start the command.
Approving once applies only to that exact request, so the next request waits again.

!!! warning "Auto-approval covers every allowed caller"
    Timed and until-stopped auto-approval approves every shell command from all locally allowed requesters and agents, not only the command that prompted it.
    Auto-approval is never saved: it ends when its time runs out, when you revoke shell access, or when the bridge stops, and a restarted bridge asks for each command again.

Auto-approval uses a monotonic local deadline, so changing the wall clock does not extend it.
In the macOS app, **Allow Without Asking…** grants auto-approval before any request arrives, and **Revoke Shell Access** ends it at any time.
In the terminal, `--shell-auto-approve-minutes` grants auto-approval for up to 60 minutes from startup, and `Ctrl+C` stops the bridge and revokes it.
Revoking clears auto-approval, rejects a waiting request, and stops every running command and handle.

### Handles

A command still running after its inline wait keeps running as a handle instead of being killed.
The reply has `state: "running"`, the handle, and the newest output so far; the agent polls with `check_shell` and stops the command with `kill_shell`.
Handles belong to the requester and agent that started them, and another caller cannot see, check, or kill them.
Checking or killing your own handle needs no approval, and a handle keeps running after the auto-approval that started it ends.
The macOS app lists every handle with its requester, agent, command preview, elapsed time, and state, and can kill each one.
A finished `check_shell` hands its output over once and then forgets the handle.
If that reply is lost or its outcome is uncertain, recover it with `request_status` for that check's request ID instead of checking again.
The bridge retains at most 16 handles.
When all 16 are retained, a new command drops the oldest finished handle that nobody has checked, together with its output, and is refused before approval if all 16 are still running.

Revoking shell access, stopping the bridge, and helper shutdown kill every handle.
Handles never survive a helper restart.
If the helper is force-killed, for example with `SIGKILL`, running handle processes are not cleaned up.
After a command finishes, the remaining processes in its process group, such as children started with `&`, are terminated, so long-running work should stay in the foreground and continue as a handle.
Deliberately detached processes, such as those started in a new session with `setsid`, may escape this cleanup, but running, stopping, and revoking still return promptly.

### Environment and Output

Commands run with the environment of your login shell, captured once when the bridge starts.
MindRoom starts your account's login shell as an interactive login shell from a minimal environment of `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`, and a `PATH` that includes the Homebrew and `~/.local/bin` directories.
Only `TMPDIR` and `LANG` come from the helper's own environment, so helper credentials are never copied.

!!! warning "Your shell profile's exports are visible to commands"
    Anything your shell profile exports, including tokens and other secrets, is visible to every approved command.
    Commands could already read the profile files that set those values, but a command that prints one sends it to the model provider like any other output.

If capture fails or takes longer than 5 seconds, commands use that minimal environment instead.
Profile changes apply the next time the bridge starts.
Commands read standard input from `/dev/null` and have no terminal, so they cannot answer password prompts, but they keep any authority the account already has without one.
Standard error is merged into standard output in the order it is written.

The bridge captures up to 10 MiB of combined output per command in private temporary files, which it removes after transfer and when it stops.
Output beyond that limit is dropped, and the result reports `output_truncated: true`.
Output that fits in one encrypted reply returns inline.
Larger output travels as an encrypted Matrix media attachment whose key is only inside the encrypted reply, and the cloud tool downloads and verifies it within the tool's configured `timeout_seconds` before returning the full text.
If the upload fails or takes longer than 30 seconds, the reply keeps the exit code, shows the newest output that fits, sets `output_truncated`, and adds a `warning`.
For agents with a workspace, MindRoom's normal [tool output files](execution-and-coding.md#common-setup-notes) apply.
A result larger than the automatic-save threshold, 50 KiB by default, is saved as a plaintext file under `mindroom_tool_outputs/` in the agent workspace and returned as a preview, and the agent can pass `mindroom_output_path` to choose the file.

### Uncertain Outcomes and Confidentiality

Uncertain shell outcomes are never retried automatically.
If `run_shell` times out, the command may still be waiting for approval, may have run, or may still be running.
The tool tells the agent to query `request_status` with the original `request_id` instead of submitting the command again.
The local journal keeps each reply as a durable receipt, and a command interrupted by a helper crash is reported with an unknown outcome and never run again.

`status` reports which of applications, folders, and shell commands are enabled, whether a shell command is waiting for approval, the remaining auto-approval time, and only the caller's own handles.
Remote status never includes a waiting command's text; only the person at the computer sees it.
A remote status reply also fits one encrypted message, so with many selected folders it can trim the folder list and set `file_roots_truncated: true`; the local status a person at the computer sees always lists every folder.
File contents and command output become model input after MindRoom decrypts them in the cloud process, so your model provider receives them.
They can also remain in agent session storage, tool traces, automatically saved workspace files, and the local command journal.
Commands and paths appear in tool-call arguments, Matrix tool traces, and approval cards, so never put secrets in them.

## Requirements

You can use the same Matrix account as your agent chat or a dedicated Matrix account for the local bridge.
The `mindroom desktop setup` command returned by `!desktop setup` uses your chat account and adds a dedicated Matrix device to it when no saved local Desktop session exists.
For manual password login, a dedicated account reduces the impact of exposing that password on the local computer.
The desktop account and the cloud MindRoom entity must use the same Matrix federation environment and must be able to exchange to-device events and media.
If the optional local desktop dependencies are missing, `mindroom desktop run` auto-installs the `desktop` extra at startup unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
To install them in advance on the computer being controlled:

```bash
uv tool install 'mindroom[desktop]'
```

macOS supports native semantic state through AXUIElement and requires Accessibility permission for state and control.
macOS screenshots require macOS 14 or newer and Screen Recording permission.
When `mindroom desktop run` starts from a terminal, macOS attributes both permissions to that terminal app and applies a new grant only after the app is quit and reopened, even if it already appears enabled; inside tmux, also restart the tmux server with `tmux kill-server`.
ScreenCaptureKit captures the exact selected window, with its process and bounds checked before capture.
Linux currently exposes screenshot-only observation and state through the explicit `primary-screen` app ID, while coordinate input through PyAutoGUI is available during a control lease.
Local `mindroom desktop` commands, including `mindroom desktop run`, currently need macOS or Linux because the saved Desktop setup and Matrix session use POSIX file locks.
Linux pixel operation currently targets an active X11 desktop because PyAutoGUI does not provide native Wayland control.
A headless or locked graphical session is not a supported target.
Read-only folders and shell commands need no application selection and no Accessibility or Screen Recording permission.
They require macOS or Linux.
Playwright extension mode requires Node.js 18 or newer, a Chromium-family browser, and the official Playwright MCP Bridge extension installed in the browser profile that MindRoom will use.
Chrome and Brave are supported by the local command through an explicit browser executable and user-data root.

## Native macOS setup

Open MindRoom and select **Computer access**, or click the **Computer access: …** status row in the menu bar.
In the requester-agent chat, run `!desktop setup` and paste its JSON setup data into **Connect**.
Review the displayed controller fingerprint, requester, and agent, then reuse the saved Matrix login or sign in.
Confirm those identities and select **Save and Connect**. Return the displayed confirmation command to the same chat, wait for the agent's confirmation, and select **I’ve Confirmed in Chat**.
In **Access**, choose allowed applications and select **Save App Access**, or add read-only folders and allow shell command requests and select **Save Folder and Shell Access**.
Then complete **Permissions**, which only applications need, and **Start**.
The top summary distinguishes a missing connection, saved setup with access off, and an active connection; it names the next action.
Incomplete app setup remains disabled across restarts and can be retried with fresh setup data without replacing the login.
Cloudflare Access authentication is supported in the app when `cloudflared` is installed.

The permission controls show Accessibility and Screen Recording readiness and link to the corresponding System Settings panes.
Start in observe-only mode, then grant a bounded local lease when control is needed.
Stop and Revoke remain available while another setup operation is waiting.
The menu displays the current mode, remaining lease, and active action without exposing its arguments.
While a shell command waits for approval, its approval card stays above the **Computer access** steps, every window section shows a **Review Command** bar, and the menu bar shows **Command waiting** with a **Review Command…** item; approval happens only on that card.
Changing configuration or restarting the app/helper does not renew control or shell auto-approval.
Once a command journal exists, saving a different controller identity is rejected before settings change.
Pending work remains bound to its original controller.
Saving settings can repair malformed local configuration or restore private file permissions; revision checks still apply.

Release builds package the existing Python provider inside a separately signed helper app with a fixed bundle identity.
The menu app owns that foreground helper through inherited private standard-I/O pipes.
There is no listening management socket or separate background daemon.
The terminal workflow remains available below.

## 1. Create the Local Desktop Device

For password login, create a dedicated Matrix user such as `@my-laptop:example.org` using your normal Matrix administration or registration flow.
By default, the login command checks the homeserver's advertised methods.
It opens Matrix SSO when available and otherwise uses password login.

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
By default, all `mindroom desktop` commands use the user-level `~/.mindroom` runtime, regardless of the current working directory.
An explicit `--config`, `--storage-path`, `MINDROOM_CONFIG_PATH`, or `MINDROOM_STORAGE_PATH` still selects another runtime and must be reused across login, pairing, and bridge startup.
The device's private Olm identity is persisted separately under `<storage>/encryption_keys/`; on Unix, MindRoom forces its per-user directory to mode `0700`.
The printed Ed25519 value is the device's public fingerprint.
Self-service pairing sends that fingerprint to the exact cloud agent through an authenticated Olm-encrypted claim, so never copy user-specific Desktop identity fields into agent YAML.
On Unix, the session file is forced to mode `0600`, and the bridge refuses to load it if group or other users can read it.
The command prints values similar to these:

```text
User: @my-laptop:example.org
Device: ABCDEFGHIJ
Ed25519: desktop-device-fingerprint
```

Normal and private agents register these public identity values through the requester-scoped chat flow below.

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

When the MindRoom runtime reaches Matrix through an internal address, set `MINDROOM_DESKTOP_MATRIX_HOMESERVER` to the public Matrix URL that local Desktop clients can reach.
This changes only the login command printed by `!desktop setup`; server-side Matrix traffic continues to use `MATRIX_HOMESERVER`.
Set `MINDROOM_DESKTOP_CLOUDFLARE_ACCESS=true` when that public URL requires Cloudflare Access so the printed login and pairing commands include `--cloudflare-access`.

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

## 2. Configure the Agent

Configure the Desktop tool on a normal or private agent without authored device identity fields:

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

The tool's `timeout_seconds`, from 1 to 120, bounds each call and each shell-output download, while `run_shell` always waits up to 120 seconds so the person at the computer has time to decide.
Add `private: {per: user_agent}` only when the agent's entire runtime and state should also be requester-private.
Desktop device identities are requester-agent scoped either way.
Each user then runs `!desktop setup` in a private Matrix room containing only that user and one Desktop-enabled agent, plus the router when it is serving the command.
The short-lived pairing code is a bearer secret, so MindRoom rejects `!desktop` when any other room member is present.
The serving bot returns one full `mindroom desktop setup` command containing the configured homeserver, a short-lived code, the agent name, and the exact pinned cloud controller identity.
Run it once; it reuses an existing local Desktop Matrix session or completes login before claiming the pairing.
For commands from older servers without `--allow-agent`, the terminal asks for the agent name shown in the setup message; it never guesses from the controller ID.
After a successful claim it saves the controller, requester, and allowed agents in the same private configuration used by the macOS app.
When setup uses `--cloudflare-access`, it also saves that authentication choice for subsequent app and terminal starts, including when reusing an older login.
The pairing code is never saved there.
An open app refreshes externally saved settings while stopped and preserves unsaved form edits.
Add `--allow-app com.apple.TextEdit` to save an app choice during terminal setup, or choose apps in **Computer access** afterward.
Repeating setup for the same controller preserves existing browser, capture, folder, and shell choices, and keeps app choices unless you supply new app IDs.
Setup for a different controller starts without saved folders or shell access.
If the saved session belongs to a different homeserver or Matrix user than the command names, setup exits without pairing; pass `--storage-path` for a separate setup or run `mindroom desktop login --replace` to replace the saved session.
Then copy the exact `!desktop confirm <code> <verification>` command it prints back to the same Matrix chat.
The separate `mindroom desktop login` and `mindroom desktop pair` commands remain available for manual recovery.
The claim travels as an authenticated Olm-encrypted to-device event, and confirmation stores the local device identity only in that requester's agent-scoped credential store.
The verification value binds confirmation to the claimed Ed25519 key, so a different device cannot pre-claim a visible pairing code and be confirmed accidentally.
Another requester or agent cannot confirm or use that record, and shared credentials are never used as a fallback.
Until pairing is confirmed, Desktop calls return a setup-required result while the agent's unrelated tools continue to work.
The Desktop tool can explain the trusted chat command, but the model cannot register or replace a device itself.
Use `!desktop rotate` to replace a device without dropping the current target before confirmation, or `!desktop disconnect confirm` to remove it.

The `desktop` tool runs in the primary agent process because it needs that live agent's Matrix device and room requester identity.
The `browser` tool keeps calls resolving to `target: desktop` in the primary process, including when `default_target: desktop` is configured.
Host-browser calls still follow the worker routing policy, even with a desktop default or configured desktop device.
Listing `browser` in `worker_tools` therefore isolates its host calls while preserving desktop control through the live Matrix context.
It is hidden from OpenAI-compatible API runs when approval policy requires Matrix approval because those runs have no Matrix approval transport.

The separate `browser` tool can still target its Playwright extension transport through its own configuration:

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

## 3. Choose Local Access

Applications, read-only folders, and shell commands are saved separately, and the bridge exposes only what is saved.

### Applications

On macOS, use exact application bundle identifiers such as `com.apple.TextEdit` or `com.brave.Browser`.
You can inspect an installed app bundle without granting MindRoom access to it:

```bash
mdls -name kMDItemCFBundleIdentifier /System/Applications/TextEdit.app
```

Add only the applications needed for the current task.
Use the special app ID `primary-screen` only when full-primary-screen observation and coordinate fallback are intentionally required.
`primary-screen` has no semantic elements.
On Linux, `primary-screen` is currently the only usable state target.

### Read-Only Folders and Shell Commands from the Terminal

Choose folders and shell access in the macOS app's **Access** step, or save them from the terminal for the same connection:

```bash
mindroom desktop access --allow-folder ~/Documents/project-notes --shell
```

Repeat `--allow-folder` to add more existing folders, use `--clear-folders` to remove every saved folder, and use `--no-shell` to turn shell requests off.
Omitted options keep the saved values, and the command prints the saved applications, folders, and shell setting.
It requires completed setup, uses the same revision check as the app, and never starts the bridge or grants auto-approval.
Changes apply the next time the bridge starts.

## 4. Run the Local Bridge

After saving setup and local access through either interface, start the bridge with:

```bash
mindroom desktop run
```

The command reads the same saved controller, allowlists, read-only folders, shell setting, browser settings, and capture settings as the macOS app.
Control leases and shell auto-approval are temporary and are never restored from saved setup.
Stop the bridge in the interface that started it before switching interfaces.
Saved changes take effect on the next start; they do not change a running bridge's authority.

Explicit flags override settings for one terminal run without changing the saved setup.
Changing the controller requires its complete identity and explicit requester, agent, and app allowlists, and such a run starts without saved folders or shell access.
For example, start with observation only and an exact app allowlist:

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
Completed journal records retain bounded desktop, folder, shell, or browser response content and screenshot or output-attachment decryption metadata in `<storage>/desktop_bridge/commands.sqlite3`, which MindRoom requires to be owner-only (`0600`) before reading.
Existing `command_journal.json` files are imported as legacy receipts; current admission and delivery state lives in SQLite.

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
The bridge continues observing after the lease expires, but every control action is rejected until a person grants another local lease.
Desktop Control can grant or revoke without restarting; the CLI requires a new invocation.

### Approve Shell Commands in the Terminal

With shell requests saved, the startup message says that each command needs your approval in this terminal.
Each request then appears with its ID, expiry, requester, agent, working directory, and command, and waits for one answer:

```text
[a]pprove once, [r]eject, [5]/[15]/[60] minutes, [u]ntil stopped:
```

The timed and until-stopped answers also approve later commands from every allowed requester and agent, as the prompt says.
Only an answer typed after the prompt counts, because earlier input is discarded, and control, formatting, and backslash characters in the request are shown escaped.
If standard input is not an interactive terminal, input reaches its end, or the bridge runs as a background job of its terminal, requests are rejected with guidance instead of waiting.
To approve commands without asking for part of a run, start it with `--shell-auto-approve-minutes`, from 1 to 60; this requires saved shell access and is never saved itself.
`Ctrl+C` stops the bridge, revokes auto-approval, and stops waiting and running shell commands.
The terminal has no separate revoke command, so stop the bridge to revoke a grant early.

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
Only Desktop Control and the local bridge terminal can grant or renew the control lease.

## 5. Agent Flow

The agent calls `list_apps` and selects an exact returned app ID.
If the selected app is not running, the agent calls `launch_app`, which requires the local control lease and returns fresh state when the app becomes accessible.
It then calls `get_app_state` and inspects the returned roles, names, actions, hierarchy, and screenshot.
It prefers a semantic action using an `element_ref` and the matching `state_id`.
The action response contains a new `state_id`, new element indexes, and a new screenshot for the next decision.
The agent uses normalized `click`, `type_text`, `scroll`, or `keypress` only when the accessibility state lacks the needed semantic control.
Coordinate input, unscoped typing, general scrolling, and keypress fallbacks require a fresh, complete, stable observation.
If state is unstable or truncated, let the UI settle and call `get_app_state` again before a fallback.
Semantic actions and element-targeted typing use separate exact-element validation.
If the bridge reports stale state, the agent calls `get_app_state` again instead of reusing the old element index or coordinate.
If an action outcome is unknown or its follow-up state is incomplete, the agent observes again and does not automatically repeat the action.
Some applications change their UI successfully and then return an accessibility error, so a fresh observation is the only safe way to resolve an unknown outcome.
If the user asks to receive the screenshot, the agent calls `desktop(action="screenshot", app="...", return_attachment=true)` and then sends the returned `attachment_id` in the same turn with `matrix_message`.

For folders, the agent calls `list_folders`, then `list_directory` and `read_file` with a returned `root_id` and relative paths, continuing a long file from `next_offset`.
For shell work, the agent sends one exact command with `run_shell`, waits for the local decision and the inline result, and polls a returned handle with `check_shell`.
The tool instructs it never to resubmit or rephrase a rejected or expired command, and to query `request_status` after a timeout or unknown outcome instead of running the command again.

For browser work, the agent first uses `browser(action="start", target="desktop")` while the local control lease is active, then calls `browser(action="tabs", target="desktop")` or `browser(action="snapshot", target="desktop")`.
The snapshot returns semantic roles, names, current values, and element references from the current page.
Existing-page control is unavailable through this pinned extension backend until stable page targeting is supported.
For app-level control, an explicitly allowlisted browser can still use the native desktop state and input path.
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

With this policy, observation remains immediately available while each `desktop` app-control action waits for the original Matrix requester to approve it.
This example does not cover the separate `browser` tool; add a browser-specific rule if browser calls also need Matrix approval.
Approval does not override an absent or expired local control lease.
Adding `run_shell` to such a rule adds a chat confirmation before the request is sent, and the command still waits for approval on the computer.

## Operations

Rotate the local desktop Matrix device with `mindroom desktop login --replace`, revoke the old device in Matrix account management, then run `!desktop rotate` and follow the new pairing command in the direct agent chat.
The `--replace` option creates a fresh saved session but cannot revoke the old device by itself.
Cloud-controller rotation is a separate case: `!desktop rotate` starts pairing but does not migrate the local command journal.
An existing journal pins the cloud user, device ID, and Ed25519 fingerprint, so pairing alone cannot resume that journal under a replacement controller.
Keep the original journal and unresolved outcomes intact; do not delete or rewrite its ownership binding.
The native app has no journal-migration action.
Terminal commands accept `--storage-path` for a separate local setup; use the same new directory for `mindroom desktop login`, `mindroom desktop setup`, and `mindroom desktop run`.
A separate setup does not transfer pending commands or their outcomes from the old journal.
A device ID or Ed25519 mismatch is a hard failure and should be treated as a rotation or possible substitution, not bypassed.
Use `Ctrl+C` to stop accepting new bridge commands and begin shutdown.
Shutdown also revokes shell auto-approval, rejects a waiting shell request, and kills running shell commands and handles.
Desktop input already dispatched through a native worker thread and active Playwright MCP calls are not preemptible, so an in-flight control can still finish before shutdown returns; observe local state before deciding whether to retry it.
For stronger isolation, run the bridge in a dedicated operating-system account and expose only a non-sensitive desktop session.

## Current Limits

Native semantic accessibility is implemented only for macOS in this version.
Playwright extension mode is limited to Chromium-family browsers, so Safari and other unsupported browsers continue to use the accessibility and scoped-screenshot path.
On macOS, window-bound screenshots and coordinate input support secondary displays when the window fits one unambiguous display.
ScreenCaptureKit captures the selected window, with process and window identity revalidated around capture.
Linux pixel operations currently target the primary display.
Global keyboard shortcut chords are intentionally unavailable because they could switch to or launch an application outside the local allowlist.
The returned accessibility tree is capped and depth-bounded, and the state reports when it was truncated.
Table and outline state prefers the rows that macOS reports as visible so off-screen Finder-style content does not crowd current controls out of the bounded tree.
There is no MatrixRTC live screen stream, multi-monitor selector, unattended service installer, or remote approval of local lease changes yet.
Folder access is read-only and text-only, with at most 16 KiB per read and 200 entries per listing.
Shell commands have no terminal, standard input, or persistent session, and their handles do not survive a helper restart.
Shell commands cannot be approved remotely, and uncertain shell outcomes are never retried automatically.
Commands and encrypted responses are Matrix to-device messages rather than persistent room events, while normal MindRoom tool traces and optional approval cards remain visible in the Matrix conversation.
