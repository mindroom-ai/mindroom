---
icon: lucide/monitor-up
---

# Matrix Desktop Bridge

The `desktop` tool lets a cloud-hosted MindRoom agent observe or operate one local computer without exposing an inbound port.
The local computer runs an outbound Matrix sync client, while commands and responses use Olm-encrypted to-device events addressed to exact pinned Matrix devices.
Screenshots are encrypted before upload to Matrix media, and their decryption keys travel only inside the encrypted response.

This first version is step-based rather than live video.
The agent requests a screenshot or performs one bounded action, and every control action returns a fresh screenshot.
Each response reports both source-screen and encoded-image dimensions so an agent can scale image coordinates back to the real desktop.

## Security Model

The local bridge starts in observe-only mode unless a person at the computer grants a short control lease on the command line.
The local process independently checks the exact cloud Matrix user, device ID, Ed25519 fingerprint, human requester ID, agent name, command expiry, request ID, and monotonic session sequence.
Cloud configuration cannot enable control, extend a running lease, or change the local allowlists.
Restarting the local bridge returns it to observe-only mode unless `--allow-control` is supplied again.
Moving the pointer to the upper-left corner triggers PyAutoGUI's emergency stop and latches control off until the local bridge is restarted.

The exposed action set is intentionally small:

- `status` reports screen geometry and cursor position.
- `screenshot` captures the active desktop.
- `click` clicks one bounded screen coordinate.
- `type_text` types up to 2,000 characters into the focused application.
- `scroll` scrolls a bounded amount at the current or supplied position.
- `keypress` presses one key or a combination of at most four supported keys.

The bridge does not expose a shell, filesystem, clipboard, microphone, webcam, unlock operation, privilege elevation, or arbitrary local RPC.
It operates only the currently logged-in graphical session and cannot bypass operating-system permission prompts.

Matrix protects the local-to-cloud transport, but a screenshot becomes model input after MindRoom decrypts it in the cloud process.
Your configured model provider can therefore receive visible screen contents, just as it receives other image inputs.
The Matrix homeserver can observe routing metadata, timing, and encrypted media size, but not the command body or screenshot plaintext.

## Requirements

Use a dedicated Matrix account for the local desktop bridge.
The desktop account and the cloud MindRoom entity must use the same Matrix federation environment and must be able to exchange to-device events and media.
Install the optional local desktop dependency on the computer being controlled:

```bash
uv tool install 'mindroom[desktop_bridge]'
```

macOS requires Screen Recording permission for observation and Accessibility permission for control.
Linux support currently targets an active X11 desktop because PyAutoGUI does not provide native Wayland control.
Windows uses the permissions of the logged-in desktop user.
A headless or locked graphical session is not a supported target.

## 1. Create the Local Desktop Device

Create a dedicated Matrix user such as `@my-laptop:example.org` using your normal Matrix administration or registration flow.
Run the one-time login on the local computer and enter that account's password at the hidden prompt:

```bash
mindroom desktop login --user-id @my-laptop:example.org
```

The command saves only the reusable Matrix access token and device identifiers under the selected MindRoom storage directory.
On Unix, the session file is forced to mode `0600`, and the bridge refuses to load it if group or other users can read it.
The command prints values similar to these:

```text
User: @my-laptop:example.org
Device: ABCDEFGHIJ
Ed25519: desktop-device-fingerprint
```

Copy these exact public identity values to the cloud MindRoom configuration.

## 2. Configure the Cloud Agent

Start cloud MindRoom at least once so the chosen agent or team has a persistent Matrix device.
On the cloud server, print that controller's local device identity:

```bash
mindroom desktop controller --entity computer
```

Copy the printed controller user, device, and Ed25519 values to the local run command in the next section.

Configure the local desktop device as an authored override on the exact cloud entity that will call the tool:

```yaml
agents:
  computer:
    display_name: Computer Agent
    role: Observe and operate my locally authorized computer one step at a time
    tools:
      - desktop:
          device_user_id: "@my-laptop:example.org"
          device_id: "ABCDEFGHIJ"
          device_ed25519: "desktop-device-fingerprint"
          timeout_seconds: 30
```

The `desktop` tool runs in the primary agent process because it needs that live agent's Matrix device and room requester identity.
It is hidden from OpenAI-compatible API runs when approval policy requires Matrix approval because those runs have no Matrix approval transport.

## 3. Run the Local Bridge

Start with observation only:

```bash
mindroom desktop run \
  --controller-user-id @computer:example.org \
  --controller-device-id CLOUDDEVICE \
  --controller-ed25519 cloud-device-fingerprint \
  --allow-requester @alice:example.org \
  --allow-agent computer
```

Every requester and agent value is an exact local allowlist entry, and the options can be repeated when more than one exact identity is needed.
Wildcards are not accepted as authority.
The process opens outbound HTTPS connections to Matrix and does not listen on a network port.

To grant keyboard and pointer control for fifteen minutes, stop the observe-only process and restart it locally with an explicit lease:

```bash
mindroom desktop run \
  --controller-user-id @computer:example.org \
  --controller-device-id CLOUDDEVICE \
  --controller-ed25519 cloud-device-fingerprint \
  --allow-requester @alice:example.org \
  --allow-agent computer \
  --allow-control \
  --lease-minutes 15
```

The maximum lease accepted by the CLI is sixty minutes.
The bridge continues running after the lease expires, but every control action is rejected until a person restarts it with a new lease.

## 4. Add Matrix Approval for Control Actions

The local lease is the hard authority boundary, while MindRoom's existing approval cards can add per-action human confirmation in the Matrix conversation.
Create `approval_scripts/desktop_control.py` beside the cloud config:

```python
CONTROL_ACTIONS = {"click", "type_text", "scroll", "keypress"}


def check(tool_name: str, arguments: dict[str, object], agent_name: str) -> bool:
    return tool_name == "desktop" and arguments.get("action") in CONTROL_ACTIONS
```

Reference that script from the cloud configuration:

```yaml
tool_approval:
  default: auto_approve
  rules:
    - match: desktop
      script: ./approval_scripts/desktop_control.py
```

With this policy, `status` and `screenshot` remain immediately available while each control action waits for the original Matrix requester to approve it.
Approval does not override an absent or expired local control lease.

## Operations

Rotate the local desktop Matrix device with `mindroom desktop login --replace`, then update all three local device pin fields in the cloud tool configuration.
If the cloud entity receives a new Matrix device, run `mindroom desktop controller --entity <name>` again and update the three controller options used locally.
A device ID or Ed25519 mismatch is a hard failure and should be treated as a rotation or possible substitution, not bypassed.

Use `Ctrl+C` to stop the local bridge immediately.
For stronger isolation, run the bridge in a dedicated operating-system account and expose only a non-sensitive desktop session.

## Current Limits

The current provider is PyAutoGUI, so it offers whole-desktop screenshots and coordinate-based input rather than semantic accessibility-tree controls.
There is no MatrixRTC live screen stream, tray application, multi-monitor selector, unattended service installer, or remote approval of local lease changes yet.
Commands and encrypted responses are Matrix to-device messages rather than persistent room events, while normal MindRoom tool traces and optional approval cards remain visible in the Matrix conversation.
