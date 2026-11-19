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
Launch Services can miss a copy, so `MindRoom Chat.app` and the old name `MindRoom.app` in `/Applications` and `~/Applications` count too when their `Info.plist` has the chat app's bundle identifier (`MindRoom.app` there is normally the menu bar app).
That covers a copy MindRoom just installed, before Launch Services has noticed it.
When a copy exists MindRoom never installs.
Detection runs when Overview appears, when the menu opens, before every open or install, and once more just before an install moves its copy into place.

Detection only finds a candidate.
Before MindRoom opens one, that exact bundle must satisfy the app's own designated requirement, checked with `kSecCSCheckAllArchitectures` and `kSecCSCheckNestedCode`:

```text
anchor apple generic and identifier "chat.mindroom.app"
and (certificate leaf[field.1.2.840.113635.100.6.1.9] exists
     or certificate 1[field.1.2.840.113635.100.6.2.6] exists
        and certificate leaf[field.1.2.840.113635.100.6.1.13] exists
        and certificate leaf[subject.OU] = "DNA6966LGZ")
```

The first branch is the Mac App Store's build, the second MindRoom's Developer ID build.
It is not strict and asks for no revocation check: an installed copy may have gained Finder attributes, and a click must not wait on the network.
A candidate that fails is not opened; the card says which path it was and to remove it.
A failure to launch is shown on the card as well.

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
Redirects are followed only over HTTPS to `api.github.com`, `github.com`, `objects.githubusercontent.com`, and `release-assets.githubusercontent.com`, and both responses must be `200`.
These checks only keep honest mistakes out; the trust decision is the signature below.

### Verify before install (90%) 🔒

The copy that is about to be installed (the staged copy, see Install) must satisfy this code requirement, checked in-process with `SecStaticCodeCheckValidity` and the flags `kSecCSCheckAllArchitectures`, `kSecCSCheckNestedCode`, `kSecCSStrictValidate`, `kSecCSRestrictSymlinks`, and `kSecCSEnforceRevocationChecks`:

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

1. Target: `/Applications/MindRoom Chat.app` when this user can write to `/Applications` and that folder is trusted, else `~/Applications/MindRoom Chat.app` (the folder is created with mode `0755`) under the same rule, else the install is refused with "… is not a folder that only you and this Mac's administrators can change." before anything is downloaded.
   A folder is trusted when it is a real folder (not a link) owned by root or this user, not writable by others, group-writable only for the admin group (gid 80), and without an access control list, as a standard `/Applications` is.
   The list's entries are not read: any list, or a failure to tell that there is none, makes the folder untrusted.
   The isolation of the staged copy rests on this: mode `0700` protects what is inside the staging folder, and only a trusted parent protects the staging folder's own name.
   The installed name is always `MindRoom Chat.app`, also for older images that hold `MindRoom.app`, which is the menu bar app's own name.
2. If a copy is detected or the target already exists, stop with "… already exists" before any download. Nothing is ever overwritten.
3. Download the image into a fresh folder under the temporary directory.
4. `hdiutil attach <image> -readonly -nobrowse -noautoopen -mountpoint <temp>/image`.
   `hdiutil` warns on macOS 27 that `diskutil image attach` replaces it, but MindRoom supports macOS 14, where only `hdiutil` is sure to exist.
   The device that `hdiutil` reports is remembered for the detach.
5. The image's `.app` must be a real folder on the image, not a link, because `ditto` follows a link it is given.
   `ditto` copies it to `<target folder>/.MindRoomChatInstall-<UUID>/MindRoom Chat`: a folder of this install's own, mode `0700`, on the target's volume, so no other user and no other install can change the copy between its check and its move.
   The name has no `.app`, so nothing treats the unchecked copy as an application.
6. Verify the staged copy (above).
7. Detect once more (a copy may have arrived, or Launch Services may have noticed one, during the download), then move the staged copy to the target with `renamex_np` and `RENAME_EXCL`: one atomic step that fails when anything is at the target.
8. On every path, success or failure: detach the device (once more when it is busy), delete this install's staging folder, and delete the temporary folder only after a detach succeeded or when nothing was attached.
   An attach that fails can still leave the image mounted: then the mount point is on another volume than its parent folder and is detached. An attach that was stopped at its time limit may have attached it: its mount point is detached too, and the temporary folder stays when that fails.
   `hdiutil` and `ditto` get five minutes, then a request to stop, then five seconds later a kill, and the install fails; their output goes to a file, so a child that keeps a pipe open cannot hold the wait. A hung tool cannot hold the install, and with it quitting, for good.

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
- verification decision: an unsigned bundle, a bundle with the right identifier signed by someone else (ad hoc), and a validly signed app of another developer (`/System/Applications/Calculator.app`) are all refused; the team and `notarized` clauses have no negative test, because that needs a notarized app of another team;
- install target: `/Applications` when writable, `~/Applications` otherwise;
- install flow with a fake tool runner: a verified app lands at the target and the image is detached and temporary files are gone; a refused app is not installed and the same cleanup happens; an existing target stops the install before any network request;
- isolation: the stage is a `0700` folder of the install's own in the target folder; a second install that is refused while the first one's copy is checked leaves that copy alone, and the published bytes are the checked bytes;
- a top-level `.app` that is a link out of the image is refused before anything is copied;
- discovery: a fresh store that Launch Services tells nothing finds a copy at each of the four known paths and starts no install; `MindRoom.app` with the menu bar app's identifier does not count; a copy noticed during the download stops the install before publishing;
- existing copies: an unsigned and an ad hoc signed bundle with the chat app's identifier are never opened (the real check), and a launch failure is reported;
- redirects are followed only to the GitHub hosts over HTTPS; a failed detach is retried by device and keeps the mount folder;
- store and menu: open when present, install then open when missing, menu titles for both states;
- trust: a standard `/Applications` is trusted; folders writable by others or by a group other than admin, folders with an access control list (an allow or a deny entry, added with `chmod +a`), missing folders, and links are not; an untrusted Applications folder is skipped for the home one, and with both untrusted nothing is downloaded;
- tools: one that ignores the request to stop is killed and reported within the limit; one whose child keeps the output open returns at once; a stopped attach is detached by mount point and its folder goes only after that succeeds; with the real `hdiutil`, an attach reported as failed after mounting is detached.

Not unit-tested: the exclusive rename against a racing writer, an access control list that cannot be read, an image that is attached but not mounted when `hdiutil` is stopped, `kSecCSRestrictSymlinks` against a signed bundle with a bad link, and the Mac App Store branch against an App Store build.

Real flow: an opt-in test (`MINDROOM_CHAT_INSTALL_E2E_DIR=<folder>`) runs the real installer against GitHub and the real signature check, with that folder standing in for `/Applications`.
It is run on a Mac (the MacBook M5) before the pull request and proves download, mount, copy, verification, and bundle identifier of the result.
The buttons themselves are not driven by a test; they are thin calls into the tested store.

## Components

| File (under `macos/MindRoom/Sources/MindRoom/`) | Purpose |
| --- | --- |
| `ChatAppRelease.swift` | Pure: release list JSON → image URL. |
| `ChatAppSignature.swift` | The two requirement strings, `verify(_:)` for a download and `verifyInstalled(_:)` for a copy that is opened. |
| `ChatAppInstaller.swift` | Target choice and the download, mount, copy, verify, rename sequence; network, tools, and verification are injectable. |
| `ChatAppStore.swift` | `@MainActor` observable state (installed path, progress text, error) with `refresh()` and `openOrInstall()`. |
| `ChatAppCard.swift` | The Overview card. |
| `StatusMenuController.swift`, `MindRoomRootView.swift`, `AppDelegate.swift` | Menu item, card placement, quit guard. |

Budget: at most 300 net product Swift lines for the first build; the review round that added isolation, discovery, and the check before opening brought it to about 360, and the last one (trusted parent, bounded tools) to about 390.

## Known limits

- Force-quitting MindRoom mid-install leaves the temporary folder, possibly a mounted, hidden image until restart, and a hidden `.MindRoomChatInstall-…` folder in the target folder, which nothing removes.
- The install needs the network twice (GitHub API, download). It asks for a revocation check, but macOS does not promise to refuse when Apple's revocation service cannot be reached.
- `/Applications` is writable by every administrator of the Mac by design; MindRoom does not refuse it for that. The private staging folder and the exclusive rename protect the copy until it is in place, not afterwards.
- The trust rule fails closed on access control lists: a folder with any entry is not used, also when the entry only denies something, as the `everyone deny delete` entry on macOS's own home folders does. Remove the list (`chmod -N`) to install there.
- Between the signature check of an existing copy and Launch Services opening it, the same user could still swap the bundle; macOS offers no way to open the bytes that were checked.
- GitHub's unauthenticated API allows 60 requests per hour per address; over that the card says no download was found.
- Launch Services decides what "installed" means, so a stray copy in an unusual folder (a backup, a build folder) counts as installed and is what **Open MindRoom Chat** opens, if its signature passes.

## Open questions for Bas

1. Should **Open Chat** (menu and Overview) open the installed app when there is one, leaving the embedded view for pairing and for Macs without the app? Recommendation: yes, as a follow-up (60%).
2. Should the pairing bar get **Open in MindRoom Chat** once the universal link is proven on a real Mac? Recommendation: not yet (70%).
