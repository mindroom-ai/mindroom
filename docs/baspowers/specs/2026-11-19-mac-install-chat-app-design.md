# MindRoom for macOS installs MindRoom Chat next to itself

Date: 2026-11-19. Status: decided, ready to build.

## Intent

On the Mac, "MindRoom" is the full product and includes MindRoom Chat.
MindRoom (the menu bar app, `chat.mindroom.menubar`) therefore offers to install MindRoom Chat (`chat.mindroom.app`, Mac Catalyst) next to itself and opens it afterwards.
MindRoom Chat stays a separate app with its own updater and Mac features.

Success means: a person who installed only MindRoom gets MindRoom Chat with one click, nobody ever ends up with two copies, and nothing that is not the real, notarized MindRoom Chat can be installed through this path.

Said by Bas: MindRoom installs MindRoom Chat next to itself; the chat app stays separate.
Assumed: the offer is explicit (a button), not automatic, and the embedded chat view stays.

## Approaches considered

1. **Download, verify, and copy the released disk image (chosen).** MindRoom fetches the newest released DMG from GitHub, checks the signature, and copies the app. Small, no new dependency, always the current release, and the chat app's own updater takes over.
2. **Ship MindRoom Chat inside `MindRoom.dmg` or inside `MindRoom.app`.** Rejected: a stale copy in every MindRoom release, about 25 MB more per download and update, a second Sparkle app nested in or beside the first, and a duplicate for everyone who already has the App Store or Homebrew copy.
3. **Hand off to Homebrew or the browser.** Rejected: most people who install from the DMG have no Homebrew, and a browser download is a manual drag with a Gatekeeper prompt, which is not "MindRoom installs it".

## Decisions

Each line ends with my confidence that it is the right call.

### Detect (90%)

`NSWorkspace.shared.urlForApplication(withBundleIdentifier: "chat.mindroom.app")`, accepted only when that path still exists.
A copy from any source counts: disk image, Homebrew, or the Mac App Store all use this bundle identifier, under whatever file name (`MindRoom.app`, `MindRoom Chat.app`).
When one exists MindRoom only opens it and never installs.
A copy MindRoom just installed counts at once, before Launch Services has noticed it.
Detection runs when Overview appears, when the menu opens, and before every open or install.

### Offer and UX (75%)

- **Overview** gets a third card, **MindRoom Chat**, with the status **Installed** or **Not installed** and one button: **Install MindRoom Chat** or **Open MindRoom Chat**.
  Overview is the first screen of a first run, so this is the first-run offer; there is no separate dialog.
- **Menu bar** gets one item under **Open Chat**: **Install MindRoom Chat…** when missing (it shows Overview, where progress appears, and starts the install), **Open MindRoom Chat** when present, and a disabled **Installing MindRoom Chat…** meanwhile.
- The install is never silent or automatic: it starts only from one of these two clicks.
- **Progress** is the current stage as text beside a spinner (finding, downloading, checking, installing).
  A percentage is left out: the image is about 25 MB, and URLSession's async download reports no byte progress without a delegate session, which is not worth its lines here.
- **Failure** shows one clear sentence on the card; the button returns so the person can retry.
- **After a successful install** MindRoom opens MindRoom Chat.
- **Settings** gets nothing: there is no preference to set.
- MindRoom asks before quitting during an install, as it does during a runtime action, so an install is not cut off halfway.

### The embedded chat view (80%)

Unchanged.
It is the zero-install way to chat, it works with a self-hosted chat website, and local-agent pairing relies on it.
**Open Chat** in the menu and on Overview keeps opening it.
Whether **Open Chat** should open the installed app instead is left to Bas (see Open questions); doing it here would silently change what an existing button does.

### Pairing approval links (75%)

Not routed to the installed app in this change.
MindRoom Chat does claim them: its `apple-app-site-association` maps `https://chat.mindroom.chat/connect` to `DNA6966LGZ.chat.mindroom.app` ("Local MindRoom pairing").
So the existing **Open in Browser** button, which calls `NSWorkspace.open`, already lands in MindRoom Chat on a Mac where macOS has verified that link; no code is needed for that.
Forcing it would break self-hosted chat websites (no universal link) and people who are signed in only in the embedded view, and I cannot prove the universal link from here.

### Source (90%)

`GET https://api.github.com/repos/mindroom-ai/mindroom-chat/releases?per_page=10`, no token.
Walk the releases newest first, skipping drafts and prereleases.
In each release take the asset named `MindRoom-Chat.dmg`, else `MindRoom.dmg`, whose `state` is `uploaded`; the first release that has one wins.
This is the rule of the tap's livecheck and of `mindroom-chat/src/app/native/macApp.ts`, extended with the new asset name.
The newest release has no image for some minutes after it is published, which is why `releases/latest/download/…` is not used.
The download URL must start with `https://github.com/mindroom-ai/mindroom-chat/releases/download/`; anything else counts as "no release found".
This check only keeps honest mistakes out; the trust decision is the signature below.

### Verify before install (90%) 🔒

The copy that is about to be installed (the staged copy, see Install) must satisfy this code requirement, checked in-process with `SecStaticCodeCheckValidity` and the flags `kSecCSCheckAllArchitectures`, `kSecCSCheckNestedCode`, `kSecCSStrictValidate`, and `kSecCSEnforceRevocationChecks`:

```text
anchor apple generic and identifier "chat.mindroom.app"
and certificate 1[field.1.2.840.113635.100.6.2.6] exists
and certificate leaf[field.1.2.840.113635.100.6.1.13] exists
and certificate leaf[subject.OU] = "DNA6966LGZ"
and notarized
```

That is: the whole bundle's signature is intact, Apple's chain, a Developer ID Application certificate, team `DNA6966LGZ`, identifier `chat.mindroom.app`, and Apple's notarization ticket.
It is the Developer ID branch of the app's own designated requirement plus `notarized`; the Mac App Store branch is left out because a download is never the App Store build.

Gatekeeper is covered by `notarized` rather than by `spctl`: `spctl --assess` accepts everything on a Mac where assessments are switched off, and its verdict would have to be parsed from tool output.
A download made by MindRoom carries no quarantine flag, so macOS itself will not check the app at first launch; this requirement is the only gate, and it does not depend on the Mac's Gatekeeper setting.
Any failure refuses the install with "The downloaded app is not the notarized MindRoom Chat from MindRoom, so it was not installed." and the system's reason.

Verifying the staged copy rather than the mounted image means the bytes that are checked are the bytes that get installed.

### Install (85%)

1. Target: `/Applications/MindRoom Chat.app` when this user can write to `/Applications`, else `~/Applications/MindRoom Chat.app` (the folder is created).
   The installed name is always `MindRoom Chat.app`, also for older images that hold `MindRoom.app`, which is the menu bar app's own name.
2. If the target already exists, stop with "… already exists" before any download. Nothing is ever overwritten.
3. Download the image into a fresh folder under the temporary directory.
4. `hdiutil attach <image> -readonly -nobrowse -noautoopen -mountpoint <temp>/image`.
   `hdiutil` warns on macOS 27 that `diskutil image attach` replaces it, but that verb does not exist on macOS 14, which MindRoom supports.
5. `ditto` the image's `.app` to `<target folder>/.MindRoom Chat.installing`, a hidden name without `.app` so Launch Services ignores it; a leftover from a killed install is removed first.
6. Verify the staged copy (above).
7. Rename it to the target. A rename fails rather than replaces, which keeps rule 2 true if something appeared meanwhile.
8. On every path, success or failure: detach the image, delete the staged copy if it is still there, delete the temporary folder.

### Updates (95%)

None from MindRoom.
MindRoom Chat updates itself (Sparkle in the Developer ID build), and Sparkle keeps the bundle's installed name.
MindRoom never touches an installed copy again.

### Homebrew (85%)

Cask `mindroom` should **not** `depends_on cask: "mindroom-chat"`.
Homebrew cannot see an App Store or disk-image copy, so the dependency would either install a duplicate or fail the whole `mindroom` install with "already an App at …".
MindRoom's own detection by bundle identifier handles all three sources.
No tap change belongs to this pull request.

### Testing (85%)

Unit tests (XCTest, `macos/MindRoom/Tests/MindRoomTests/ChatAppInstallTests.swift`):

- release picking: newest release with an image wins; drafts, prereleases, releases without an image, and assets that are not `uploaded` are skipped; `MindRoom-Chat.dmg` beats `MindRoom.dmg` in one release; the old name alone still works; a list that is not a list or a URL outside the repository's downloads gives nothing;
- verification decision: an unsigned bundle and a validly signed app of another developer (`/System/Applications/Calculator.app`) are both refused;
- install target: `/Applications` when writable, `~/Applications` otherwise;
- install flow with a fake tool runner: a verified app lands at the target and the image is detached and temporary files are gone; a refused app is not installed and the same cleanup happens; an existing target stops the install before any network request;
- store and menu: open when present, install then open when missing, menu titles for both states.

Real flow: an opt-in test (`MINDROOM_CHAT_INSTALL_E2E_DIR=<folder>`) runs the real installer against GitHub and the real signature check, with that folder standing in for `/Applications`.
It is run on a Mac (the MacBook M5) before the pull request and proves download, mount, copy, verification, and bundle identifier of the result.
The buttons themselves are not driven by a test; they are thin calls into the tested store.

## Components

| File (under `macos/MindRoom/Sources/MindRoom/`) | Purpose |
| --- | --- |
| `ChatAppRelease.swift` | Pure: release list JSON → image URL. |
| `ChatAppSignature.swift` | The requirement string and `verify(_:)`. |
| `ChatAppInstaller.swift` | Target choice and the download, mount, copy, verify, rename sequence; network, tools, and verification are injectable. |
| `ChatAppStore.swift` | `@MainActor` observable state (installed path, progress text, error) with `refresh()` and `openOrInstall()`. |
| `ChatAppCard.swift` | The Overview card. |
| `StatusMenuController.swift`, `MindRoomRootView.swift`, `AppDelegate.swift` | Menu item, card placement, quit guard. |

Budget: at most 300 net product Swift lines.

## Known limits

- Force-quitting MindRoom mid-install leaves the temporary folder and possibly a mounted, hidden image until restart; the staged copy is removed by the next install.
- The install needs the network twice (GitHub API, download) and Apple's revocation check; behind a proxy that blocks the latter the install is refused rather than weakened.
- GitHub's unauthenticated API allows 60 requests per hour per address; over that the card says no download was found.
- Launch Services decides what "installed" means, so a stray copy in an unusual folder (a backup, a build folder) counts as installed and is what **Open MindRoom Chat** opens.

## Open questions for Bas

1. Should **Open Chat** (menu and Overview) open the installed app when there is one, leaving the embedded view for pairing and for Macs without the app? Recommendation: yes, as a follow-up (60%).
2. Should the pairing bar get **Open in MindRoom Chat** once the universal link is proven on a real Mac? Recommendation: not yet (70%).
