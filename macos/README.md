# MindRoom for macOS

The native app and its desktop helper require macOS 14 or newer.

The menu app includes a Python desktop helper at:

```text
MindRoom.app/Contents/Helpers/MindRoom Desktop Helper.app
```

The helper has the fixed bundle identifier `chat.mindroom.desktophelper`. It runs the existing Python Accessibility, screen capture, input, browser, Matrix, and desktop bridge implementations. The menu app launches it as a foreground child and communicates only through inherited stdin, stdout, and stderr pipes. Closing the app closes that channel and clears every control lease.

## Build

```bash
macos/build-macos-app.sh
macos/build-macos-app.sh --universal --dmg
```

`build-macos-app.sh` invokes `build-desktop-helper.sh`, which creates a PyInstaller onedir app in an isolated uv environment. It copies the helper into the parent, stamps matching versions, signs the helper before the parent, and runs `verify-desktop-helper.sh`. The helper build does not modify the project environment.

For a universal release, `HELPER_PYTHON` may select a Python 3.13 executable containing both arm64 and x86_64 slices. The build fails before PyInstaller when that interpreter is not universal, then verifies both slices in every Mach-O file collected into the helper. Native dependencies must also provide both slices.

## Runtime contract

The helper entry point requires an explicit `--config` value. An optional `--storage-path` overrides normal runtime resolution; when omitted, configured process or adjacent `.env` storage is preserved. Stdout is reserved for protocol-version-1 NDJSON. Library diagnostics are redirected to stderr. Requests use UUID correlation IDs; input lines are limited to 65,536 bytes and output lines to 262,144 bytes.

The Desktop Control window can import the structured setup descriptor copied from a direct agent chat. Import only fills transient form state. The one-time pairing code is not written to the native configuration. The person at the Mac must confirm the exact controller fingerprint, requester, and agent after edits or configuration revision changes.

Ordinary setup mutations are serialized and the stdio server admits at most four concurrently queued regular requests. Stop has a separate single request lane; status, revoke, and emergency reset remain available while a login, pairing, browser, or stop request is pending. Excess requests receive an immediate retryable `busy` response. A durable transport or bridge worker failure fences admission and control before its peer is cancelled.

Copied diagnostics exclude stderr, setup descriptors, pairing codes, Matrix identities, URLs, request parameters, and credentials.

## macOS release gates

Portable tests cover protocol bounds, durable configuration, setup import, lifecycle transitions, urgent revoke, transport failure fencing, redaction, and EOF shutdown. A release still requires these checks on macOS:

1. `swift test --package-path macos/MindRoom`.
2. Build the universal app and verify both nested and parent signatures.
3. Confirm Accessibility and Screen Recording prompts name the packaged helper.
4. Upgrade over a prior signed build and confirm permission continuity.
5. Exercise keyboard and VoiceOver navigation in Desktop Control.
6. Start and stop the installed-profile browser extension and verify existing tabs remain outside control.
7. Notarize, staple, and launch the DMG build, then test a Sparkle update.
