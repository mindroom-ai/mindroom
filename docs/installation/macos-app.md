# macOS App

MindRoom provides a native macOS window with a small menu bar companion.
Open the app from Applications or Spotlight to manage two independent roles:

- **Local agents** run MindRoom on this Mac and connect to your Matrix chat account.
- **Computer access** lets a paired agent running elsewhere observe or control selected applications, read selected folders, and request shell commands that you approve on this Mac.

Use either role, or both.
Starting local agents does not enable computer access, and starting computer access does not start local agents.
The app bundles the official M SVG from `assets/logo/logo-mark.svg` and its generated PNG companion for native display.

## Requirements

- macOS 14 Sonoma or later, on Apple silicon or Intel.
- Network access to install the MindRoom runtime and connect to your Matrix server.
- For local agents, a configured model provider credential, local model, or supported provider login.
- For computer access, an existing Desktop-enabled MindRoom agent; application access also needs the macOS permissions described in the [Desktop guide](../tools/desktop.md).

## Install

```bash
brew install --cask mindroom-ai/tap/mindroom
```

Open **MindRoom** from Applications or Spotlight.
The window has **Overview**, **Local agents**, **Computer access**, and **Settings** sections.
Overview explains the two roles and shows their status separately.

## Set Up Local Agents

Open **Local agents** and expand **Set up or reconnect local agents**.

1. **Install MindRoom** installs the command-line runtime using the bundled `uv`.
2. **Prepare Configuration** creates missing `config.yaml` and `.env` files in `~/.mindroom`, preserves an existing config and env values, and appends missing hosted Matrix defaults to `.env`.
   **Open MindRoom Chat**, sign in, and use **Local MindRoom** in the chat sidebar to generate a pair code.
   Enter the code and choose **Pair Account**.
3. **Open Config Folder** and configure your AI provider in `.env`, or configure a local model in `config.yaml`.
4. **Install and Start Agents** installs and starts the launchd background service.

Commands show progress and their results in the window.
Once the service is running, **Configure Agents…** opens the existing web dashboard at `http://localhost:8765`.
Use that dashboard for agent and model configuration.
**Open Chat** opens `https://chat.mindroom.chat` in your browser.

### Where do my agents run?

Your agents run on this Mac.
The hosted `mindroom.chat` Matrix server relays their chat messages.
Signing in to `chat.mindroom.chat` creates your hosted Matrix account; it does not move the agent runtime into the cloud.

### Your own Matrix server

Under **Use your own Matrix server**, choose **Prepare Self-Hosted Configuration**, then edit `config.yaml` and `.env` for that server and your model provider.
Start and manage the Matrix server separately from this app.
The initialization actions explicitly target `~/.mindroom`.

## Computer Access

Computer access uses the bundled Desktop Helper and does not require the local-agent runtime or background service.
Open **Computer access** to manage its session independently.

The connection summary and four steps stay at the top of the section: **Connect**, **Access**, **Permissions**, and **Start**.
Each step shows a green check when complete and a short status below its name.
Unsaved access choices, nothing saved, or missing permissions for saved applications show an amber indicator.
Start is checked only while access is running.
A new setup shows **Not connected**.
A saved setup shows **Connection saved · Access off** and opens the next incomplete step; **Connected** means computer access is running.

1. In a private chat with your Desktop-enabled agent, send `!desktop setup` and copy its JSON setup data.
2. In **Connect**, paste it into **Setup data** and select **Import Setup**.
3. Review the controller fingerprint, requester, and agent. The app reuses a matching saved Matrix login; otherwise choose **Sign In with Browser**, or expand the password option.
4. Confirm the displayed identities and select **Save and Connect**.
5. Copy the displayed confirmation command into the same agent chat. After the agent confirms pairing, select **I’ve Confirmed in Chat**.
6. In **Access**, choose **Applications**, **Read-only folders**, or **Shell commands**, and save each choice.
   For applications, search and check the applications to allow, then select **Save App Access** above the list.
   Search accepts partial names and reordered words, such as `chr goo` for Google Chrome.
   For folders and shell commands, see [Folders and Shell Commands](#folders-and-shell-commands) and select **Save Folder and Shell Access**.
7. If you saved applications, allow Accessibility and Screen Recording for this copy of MindRoom in **Permissions**.
   Restart the app if macOS requires it, then select **Check Again**.
   Folder and shell access need neither permission, so **Permissions** shows **Not needed** without saved applications.
8. Select **Start Observe Only**, or **Start Access** when folders or shell commands are saved.
   The summary changes to **Connected** and offers **Stop Access**.

Setup stays disabled until you acknowledge the chat confirmation. If the app closes before that step, request fresh setup data and repeat Connect; the saved login remains available.
For homeservers behind Cloudflare Access, the app opens the organization sign-in flow when needed. This requires `cloudflared` installed on the Mac; missing-helper errors explain how to install it.

The terminal `mindroom desktop setup` command saves the same connection used by the app.
An already open app refreshes that setup automatically while computer access is stopped, preserving unsaved edits.
After confirming pairing in chat, choose and save access here or with `mindroom desktop access`, then start from either the app or `mindroom desktop run`.
Stop the bridge in the interface that started it before starting it in the other interface.
Terminal setup can also save app choices with repeated `--allow-app` options; omitting them preserves choices for the same controller.
Both interfaces default to `~/.mindroom`; a terminal `--config` or `--storage-path` override creates a separate setup.
The existing native helper owns authentication, pairing, permissions, browser sessions, and control leases.

The summary always names the next required action. The **Start** step explains any remaining blocker and links to its step.
You can inspect any step without scrolling through the other steps.
Access choices made before connecting are retained through setup.
Once setup is saved, **Save App Access** updates app selections, and **Save Folder and Shell Access** updates folders and shell requests, each without changing the other.
A saved browser setting alone does not enable Start; save an application, folder, or shell choice first.

Permission status applies to the running copy of MindRoom.
If System Settings already shows MindRoom enabled but the app reports **Not allowed yet**, quit and reopen MindRoom first.
Replacing the signed release with a local build can invalidate the saved approval while leaving the old entry enabled.
In that case, reinstall the signed release or remove the old permission entry and approve the current copy in System Settings, then select **Check Again**.

Application observation and control are separate choices.
**Grant Control…** shows the saved identities, allowed applications, and duration for explicit confirmation.
**Revoke Now** immediately removes input authority while observation continues; **Stop Access** ends the bridge session.
Control expires according to the helper's bounded lease and is never renewed automatically at app launch or restart.
The menu also provides an immediate control-revoke action while a lease is active, and **Revoke Shell Access** while shell auto-approval or a shell command is active.
Optional browser settings and redacted diagnostics are available in expandable sections.
See the [Desktop guide](../tools/desktop.md) for macOS permissions, pairing recovery, and browser-extension setup.

### Folders and Shell Commands

In **Access**, **Read-only folders** lists the folders an agent may list and read; **Add Folder…** chooses more and **Remove** drops one.
Agents cannot create, change, or delete files there.
macOS may still ask before MindRoom reads protected folders such as Desktop, Documents, or Downloads, and MindRoom never requests Full Disk Access.
**Shell commands** has one switch, **Allow shell command requests**.
Save both with **Save Folder and Shell Access**; while access is running, **Stop and Save…** stops it first after confirmation.

!!! warning "Shell commands run with your full macOS account access"
    An approved command can use the network and every file your account can read or change, including files outside the read-only folders.
    The working folder does not confine it.

While access runs with shell requests allowed, each command request appears on an approval card above the steps unless you have already chosen **Approve & Allow…** or **Allow Without Asking…** for it.
The card shows the command, working folder, agent, requester, and expiry, with control and text-direction characters shown escaped.
Choose **Reject**, **Approve Once**, or **Approve & Allow…** with **5 Minutes**, **15 Minutes**, **60 Minutes**, or **Until I Stop**.
Without a waiting request, **Allow Without Asking…** offers the same durations.
**Approve & Allow…** and **Allow Without Asking…** ask for confirmation first and list the agents and requesters the choice covers.

!!! warning "Auto-approval covers every allowed agent and requester"
    While auto-approval lasts, every shell command from all locally allowed agents and requesters runs without asking.
    It is never saved, so restarting MindRoom or its computer access asks for each command again.

**Revoke Shell Access** ends auto-approval, rejects a waiting request, and stops running commands.
**Background commands** lists commands that kept running after their first reply, with their agent, requester, elapsed time, and state, and a **Kill** button for each running one.
While a command waits, a **Review Command** bar appears in every window section, and the menu bar shows **Command waiting** with **Review Command…**.
The menu only opens the card; it never approves a command.
Stopping computer access or quitting MindRoom also revokes auto-approval and stops every shell command.
See [Shell Commands](../tools/desktop.md#shell-commands) for handles, output limits, the login-shell environment, and what the model provider receives.

## Window, Menu Bar, and Login

Opening MindRoom presents its window and Dock icon.
Closing the window leaves the app available in the menu bar and keeps its background work running.
Reopen the window with **Open MindRoom…** in the menu or by opening the application again.

The menu shows local-agent and computer-access status as clickable shortcuts to their app sections, plus chat, start/stop, settings, and quit actions.
**Service running** reports the local process state; use Chat or the dashboard to confirm that agents are ready.

In **Settings**, **Open menu bar app at login** launches the menu app quietly.
Local agents use their own launchd service and start independently at login.
Computer access always requires an explicit start in the app.

**Quit MindRoom** stops computer access owned by the app and closes its menu.
Hover over that item for a reminder of its effect on background work.
The local-agent launchd service keeps running after the app quits.
Use **Stop Local Agents** when you want to stop that service.
If a runtime action is still in progress, let it finish before quitting.

## Updates and Troubleshooting

**Settings** separates app updates from runtime updates.
**Check App Updates…** uses Sparkle for signed releases configured with an update feed.
**Update Local Runtime** updates the installed CLI.
Afterward, **Apply Runtime to Service…** rewrites the version-pinned launchd service and starts or restarts local agents after confirmation.
App updates include the bundled Desktop Helper; updating the local-agent CLI does not replace that helper.

**Open Logs Folder** opens `~/Library/Logs/mindroom`.
Background services disable terminal colors; redirected output and runtime log files use plain text unless JSON logging is configured.
The service appends to its existing logs, so records written by older versions may still contain terminal escape codes.
**Open Config Folder** opens `~/.mindroom`, which is shared with the CLI.
Failed local-agent actions show their output in the window with a copy action.
If the dashboard cannot be opened, start the service and check its logs for missing provider credentials or startup errors.
If pairing expires, generate a new code in the relevant chat flow.

Homebrew users can also update the app with:

```bash
brew update
brew upgrade --cask mindroom
```

## Uninstall

```bash
brew uninstall --cask mindroom
```

Use `brew uninstall --zap --cask mindroom` to also remove app preferences and logs.
Uninstall and zap preserve `~/.mindroom` and the uv-installed runtime.
Remove configuration, credentials, and agent data only when you intend to delete them.
Use `uv tool uninstall mindroom` to remove the CLI separately.
