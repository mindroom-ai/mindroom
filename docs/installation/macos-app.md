# macOS App

MindRoom provides a native macOS window with a small menu bar companion.
Open the app from Applications or Spotlight to manage two independent roles:

- **Local agents** run MindRoom on this Mac and connect to your Matrix chat account.
- **Computer access** lets a paired agent running elsewhere observe or control selected applications on this Mac.

Use either role, or both.
Starting local agents does not enable computer access, and starting computer access does not start local agents.
The app bundles the official M SVG from `assets/logo/logo-mark.svg` and its generated PNG companion for native display.

## Requirements

- macOS 14 Sonoma or later, on Apple silicon or Intel.
- Network access to install the MindRoom runtime and connect to your Matrix server.
- For local agents, a configured model provider credential, local model, or supported provider login.
- For computer access, an existing Desktop-enabled MindRoom agent and the macOS permissions described in the [Desktop guide](../tools/desktop.md).

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

1. In a private chat with your Desktop-enabled agent, send `!desktop setup` and copy its JSON setup data.
2. In **Computer access**, choose the allowed applications and select **Continue Setup**.
3. Paste the copied data into the **Setup data** field under **Setup or reconnect** and select **Import Setup**.
4. Review the controller fingerprint, requester, and agent, then sign in or use the saved Matrix device.
5. Confirm the displayed identities and select **Save Setup**, then confirm the saved identities again and select **Claim Pairing**.
6. Send the displayed `!desktop confirm ...` command back to the same chat.
7. Check macOS permissions and start observation.

For a homeserver behind Cloudflare Access, complete the Terminal authentication flow shown in chat first.
The existing native helper owns authentication, pairing, permissions, browser sessions, and control leases.

The session card explains unavailable Start and Stop buttons and provides shortcuts to the next required step.
If you select apps before saving a connection, **Continue Setup** opens the connection form; **Save Setup** saves your selected apps with that connection.
Once setup is saved, **Save App Access** updates app selections independently.

Permission status applies to the running copy of MindRoom.
If System Settings already shows MindRoom enabled but the app reports **Not granted to this copy**, quit and reopen MindRoom first.
Replacing the signed release with a local build can invalidate the saved approval while leaving the old entry enabled.
In that case, reinstall the signed release or remove the old permission entry and approve the current copy in System Settings, then select **Check Again**.

Observation and control are separate choices.
**Grant Control…** shows the saved identities, allowed applications, and duration for explicit confirmation.
**Revoke Now** immediately removes input authority while observation continues; **Stop** ends the bridge session.
Control expires according to the helper's bounded lease and is never renewed automatically at app launch or restart.
The menu also provides an immediate control-revoke action while a lease is active.
Optional browser settings and redacted diagnostics are available in expandable sections.
See the [Desktop guide](../tools/desktop.md) for macOS permissions, pairing recovery, and browser-extension setup.

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
