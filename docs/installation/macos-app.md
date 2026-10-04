# macOS App

The MindRoom macOS app is a native window with a menu bar companion for two independent roles:

- **Local agents** run MindRoom on this Mac and connect to your Matrix chat account.
- **Computer access** lets a paired agent running elsewhere observe or control selected applications, read selected folders, and request shell commands that you approve on this Mac.

Use either role, or both; starting one does not start the other.

## Requirements

- macOS 14 Sonoma or later on an Apple silicon Mac; Intel Macs are not supported.
- Network access to install the MindRoom runtime and reach your Matrix server.
- For local agents, a model provider credential, local model, or supported provider login.
- For computer access, a Desktop-enabled MindRoom agent; application access also needs the macOS permissions in the [Desktop guide](../tools/desktop.md).

## Install

```bash
brew install --cask mindroom-ai/tap/mindroom
```

Open **MindRoom** from Applications or Spotlight.
The window has **Overview**, **Chat**, **Dashboard**, **Local agents**, **Computer access**, and **Settings** sections, and Overview shows the status of each role.

## Chat in the App

**Chat** opens your chat client inside the app.
Sign in once; the app remembers that session separately from your browser, and switching sections keeps your conversation and draft.
The default chat website is `https://chat.mindroom.chat`; for a self-hosted client, enter an HTTPS URL or a loopback HTTP URL in **Settings** > **Chat website** and select **Save Chat Website**.
Use **Open in Browser** to use your browser session or a sign-in provider that requires a browser.
**Reload** returns to the configured chat website, or reloads the approval page while you connect local agents.

## Set Up Local Agents

**Local agents** has four numbered steps, each with its own status.
The app opens at the first step that needs attention: **Install** when the runtime needs installing or updating, **Check** when configuration exists without a service, and **Start** when a service is installed.

1. **Install** finds an existing `mindroom` executable, including a terminal installation, or installs the runtime release that matches the app version with **Install MindRoom**.
   When the installed runtime is a different release, for example after an app update, it shows **Update needed** and **Update MindRoom**.
   Configuring, pairing, checking, and installing the service wait until the runtime matches; starting, stopping, and restarting an installed service stay available.
   Installing the runtime does not install the background service.
2. **Configure** uses the installed service's saved configuration, or `~/.mindroom/config.yaml` for a new service, and shows the path.
   **Prepare Configuration** creates missing files and keeps existing configuration and credentials.
   Under **Connect or reconnect your chat account**, **Connect Account** opens the approval page in the **Chat** section.
   Check that the code above Chat matches the page and that the signed-in account is yours, then click **Approve**; **Cancel** stops waiting without saving credentials.
   An unapproved code expires after 10 minutes; choose **Connect Account** again for a new one.
   Pairing a Mac that is already connected asks first, because it creates a new connection and a new agent namespace while existing agents keep working.
   Use **Open Config Folder** to add AI provider credentials to the adjacent `.env` or configure a local model in `config.yaml`.
3. **Check** (**Check Setup**) runs `mindroom doctor` on the displayed configuration and the service's storage path, covering configuration, providers, Matrix connectivity, and storage.
   **Files found** only means configuration exists; **Passed last check** means a check in this app session had no failures or warnings.
   Warnings show **Needs attention** with a summary, and **Command output** has details.
   Editing `config.yaml` or `.env` clears the result; rerun the check after editing included files or changing external services.
4. **Start** offers **Install and Start Agents** when the launchd service is missing, **Start Agents** when it is stopped, and stop, restart, chat, and dashboard controls when it runs.
   A check is recommended but not required.
   An installed service always uses its saved configuration path, including a custom path chosen in the terminal.
   If the service starts before your account is connected, the section shows **Waiting for your chat account** until you choose **Connect Account** and approve.

**Refresh Status** rereads the runtime, configuration, and service state, including changes made in a terminal.
These controls manage only this Mac's launchd service.
**Service running** means the process is running; use Chat or the dashboard to confirm agents are ready.

### Where Do My Agents Run?

Your agents run on this Mac.
The hosted `mindroom.chat` Matrix server only relays their chat messages, and signing in to `chat.mindroom.chat` creates your hosted Matrix account without moving agents to the cloud.
For pairing details, see [Hosted Matrix](../deployment/hosted-matrix.md).

### Your Own Matrix Server

Under **Use your own Matrix server**, choose **Prepare Self-Hosted Configuration**, then edit `config.yaml` and `.env` for your server and model provider.
Run the Matrix server separately from this app.

### Open the Dashboard

While the service runs, **Open Dashboard** opens the local dashboard in the app for agent and model configuration.
The app signs in with `MINDROOM_API_KEY` from `~/.mindroom/.env` and uses `MINDROOM_URL` from the same file, defaulting to `http://127.0.0.1:8765`.
`MINDROOM_URL` must be an HTTP loopback URL (`localhost`, `127.0.0.1`, or `[::1]`) with an explicit port; otherwise the app reports `Local dashboard address is invalid. Check MINDROOM_URL and retry.`
The key is sent only to this Mac's MindRoom service, so if the dashboard cannot connect while the service is starting, select **Reload**.
After changing `MINDROOM_URL` or `MINDROOM_API_KEY`, select **Reload** in Dashboard.
The dashboard session is separate from Chat and ends when the app quits.

## Computer Access

Computer access uses the Desktop Helper bundled with the app and does not need the local-agent runtime or service.
The **Computer access** section has a summary and four steps: **Connect**, **Access**, **Permissions**, and **Start**.
The summary shows **Not connected**, **Connection saved · Access off**, or **Connected**, and always names the next required action.

1. In a private chat with your Desktop-enabled agent, send `!desktop setup` and copy its JSON setup data.
2. In **Connect**, paste it into **Setup data** and select **Import Setup**.
3. Check the homeserver, Matrix account, controller fingerprint, requester, and agent before signing in, because the setup data chooses which server receives your sign-in.
   The app reuses a matching saved Matrix login; otherwise choose **Sign In with Browser** or the password option.
4. Confirm the values again and select **Save and Connect**.
5. Copy the displayed confirmation command into the same agent chat, and after the agent confirms pairing, select **I’ve Confirmed in Chat**.
   If the app closes before this step, request fresh setup data and repeat **Connect**; the saved login is kept.
6. In **Access**, choose **Applications**, **Read-only folders**, or **Shell commands**, and save each choice.
   For applications, search and check the ones to allow, then select **Save App Access**; search accepts partial and reordered words, such as `chr goo` for Google Chrome.
   The list shows running apps and apps in standard folders; use **Add App…** to choose an application from another location.
   For folders and shell commands, see [Folders and Shell Commands](#folders-and-shell-commands).
7. If you saved applications, allow Accessibility and Screen Recording in **Permissions** with **Request** or **Open Settings**, restart the app if macOS requires it, and select **Check Again**.
   Without saved applications, **Permissions** shows **Not needed**.
8. Select **Start Observe Only**, or **Start Access** when folders or shell commands are saved.

Start needs at least one saved application, folder, or shell choice.
**Save App Access** and **Save Folder and Shell Access** each update their own choices without changing the other.
For homeservers behind Cloudflare Access, install `cloudflared`; see [Homeservers Behind an Identity-Aware Proxy](../tools/desktop.md#homeservers-behind-an-identity-aware-proxy).

### Control Applications

Applications start observe-only.
**Grant Control…** shows the identities, allowed applications, and duration for confirmation, and control ends when that lease expires; it is never renewed at launch or restart.
**Revoke Now** removes input authority while observation continues, and **Stop Access** ends the session.
Controlling a terminal, a scripting or automation app, the primary screen, or a browser or Matrix client signed in as you gives the agent your own authority; see [Security Model](../tools/desktop.md#security-model).
Optional browser settings and redacted diagnostics are in expandable sections; see the [Desktop guide](../tools/desktop.md#use-the-signed-in-browser-profile) for browser-extension setup.

### Folders and Shell Commands

In **Access**, **Read-only folders** lists the folders agents may list and read; **Add Folder…** adds one and **Remove** drops one.
**Shell commands** has one switch, **Allow shell command requests**.
Save both with **Save Folder and Shell Access**; while access runs, **Stop and Save…** stops it first after confirmation.
Approved shell commands run with your full macOS account access; read [Shell Commands](../tools/desktop.md#shell-commands) before enabling them.

While access runs with shell requests allowed, each request appears, unless auto-approval is active, on an approval card in the window and in a separate approval window that does not take keyboard focus.
Closing that window or choosing **Later** leaves the request waiting until it expires.
**Approve Once** and **Approve & Allow…** become available one second after a request appears.
Choose **Reject**, **Approve Once**, or **Approve & Allow…** with **5 Minutes**, **15 Minutes**, **60 Minutes**, or **Until I Stop**; without a waiting request, **Allow Without Asking…** offers the same durations.
Both auto-approval choices ask for confirmation and list the agents and requesters they cover; see [Local Approval](../tools/desktop.md#local-approval) for what auto-approval allows.
While a command waits, every window section shows a **Review Command** bar and the menu bar shows **Command waiting** with **Review Command…**, which opens the card but never approves.
**Revoke Shell Access** ends auto-approval, rejects a waiting request, and stops running commands.
**Background commands** lists commands still running after their first reply, with a **Kill** button for each.

### Use the Terminal with the App

The app and `mindroom desktop setup` share the same saved setup in `~/.mindroom`; an open app picks up terminal setup while access is stopped.
You can save folders and shell access with `mindroom desktop access` and start from either the app or `mindroom desktop run`.
Stop the bridge in the interface that started it before starting it in the other.
A terminal `--config` or `--storage-path` override creates a separate setup that the app does not use.

### Fix Computer Access Problems

- **Permission shows Not allowed yet although System Settings shows MindRoom enabled**: quit and reopen MindRoom.
  If you replaced the signed release with a local build, reinstall the signed release or remove the old permission entry and approve the current copy, then select **Check Again**.
- **Access fails to start**: **View Details** shows the full error, recovery advice, and redacted diagnostics to copy for support.
- **Connection or TLS certificate errors**: your saved setup is kept, so check the network or update the app and select **Start Access** again; you do not need to sign in or pair again.

See the [Desktop guide](../tools/desktop.md) for pairing recovery and device rotation.

## Window, Menu Bar, and Login

Closing the window keeps the app and its background work running in the menu bar; reopen it with **Open MindRoom…** or by opening the app again.
The menu shows local-agent and computer-access status as shortcuts to their sections, plus chat, dashboard, start and stop, **Revoke Computer Control** while a control lease is active, **Revoke Shell Access** while shell auto-approval or a command is active, settings, and quit actions.

**Settings** > **Open menu bar app at login** launches the menu app at login.
Local agents start at login through their own launchd service, while computer access always needs an explicit start in the app.

**Quit MindRoom** stops computer access but leaves the local-agent service running; use **Stop Local Agents** to stop it.
Let a running runtime action finish before quitting.

## Updates

**Settings** separates app updates from runtime updates:

- **Check App Updates…** updates the app, including the bundled Desktop Helper.
- **Update Local Runtime** installs the runtime release that matches the app version; `uv tool upgrade mindroom` does not move the runtime off that version.
- **Apply Runtime to Service…** reinstalls the background service on the updated runtime and starts or restarts local agents after confirmation.

Homebrew users can also update the app with:

```bash
brew update
brew upgrade --cask mindroom
```

## Logs and Troubleshooting

**Open Logs Folder** opens `~/Library/Logs/mindroom`, and **Open Config Folder** opens the folder holding the local agents' configuration, `~/.mindroom` by default, which the CLI shares.
Failed local-agent actions show their output in the window with a copy action.
If the dashboard does not open, start the service and check its logs for missing provider credentials or startup errors.

## Uninstall

```bash
brew uninstall --cask mindroom
```

Use `brew uninstall --zap --cask mindroom` to also remove app preferences and logs.
Neither removes `~/.mindroom`, which holds configuration, credentials, and agent data, or the runtime; remove the runtime with `uv tool uninstall mindroom`.
