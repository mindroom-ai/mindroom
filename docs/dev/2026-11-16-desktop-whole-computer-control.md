# Desktop whole-computer control

This record keeps the reasons behind the session-only whole-computer grant for the macOS desktop bridge.
What the grant does for a user is on the [Desktop Bridge](../tools/desktop.md#grant-whole-computer-control) page.

## Decisions

- The grant is runtime state on the helper's local channel, like a control lease and shell auto-approval, and never a saved field.
  A saved "all apps" setting would change the strict saved configuration format and every reader and writer of it, and it would expose every app again at each later start, far from the moment the person decided.
- Observation and control of unsaved apps come and go together as one grant.
  Splitting them would leave a second, longer-lived consent that reads every app's accessibility tree and screen while the person believes control has ended.
- Reusing `primary-screen` with a longer lease was rejected: it gives the agent pixels without an element tree, cannot launch or focus apps, and would leave the agent no reliable way to work across apps.
- A deny list of dangerous apps under whole-computer scope was rejected: any app that can run code, such as an IDE, a browser, or Finder opening a script, defeats it, so it would promise a boundary that does not exist.
  The confirmation states the account-level reach instead.
- A second executor lane for screen observations was rejected: it would need a thread-safe accessibility state store and would let a capture run concurrently with input, for a wait that the per-read deadline already bounds.
- `mindroom desktop run` gets no whole-computer flag: the terminal has no revoke surface besides Ctrl+C, and its shell approval prompt lives in a terminal window that screen input can reach.
- The menu bar title and item name whole-computer control while it is in force, so the person can always see it and end it without opening the main window.

## Authority generations and scope

- A grant that changes the scope while control is in force starts a new authority generation, like a grant after control ended.
  Control queued under one scope therefore never runs under another, even for an app both scopes cover, which keeps the rule a single comparison instead of a per-app check.
- A grant that changes the scope is refused while app input is inside its native call.
  The provider's input gate is one switch for all apps, so it cannot narrow running input to the new scope; refusing keeps the scope in force for running input exactly the one it was admitted under.
  The comparison uses the last granted scope, not the available one, because a lapsed timed lease lets running input finish and must not let a quick grant of another scope take over that input.
- The backend's scope flag is the only state the bridge writes from its own thread while a worker may be reading.
  Retained states of apps the scope no longer admits are dropped on the worker's next use instead of at the scope change, so no worker ever sees the state store change under it.
- Revoke clears the backend's scope at once, so no new read of an unsaved app starts; a lapsed lease or a latched emergency stop reaches the backend at the next executed command.
- A read already running when the scope ends still finishes on the Mac, so the bridge checks the scope again before it replies and returns nothing from an app the scope no longer admits.

## Limits

- Whole-computer control is not a sandbox, and MindRoom's window protection is not a boundary against an agent that drives Terminal or a script editor with the person's whole account.
- MindRoom's shell approval panel does not activate, so a frontmost-app check cannot see it; agent input is refused while an approval waits, in every scope, and approval waits until running input ends.
- Wider key chords, secondary-display screen targets, installed but not running apps in `list_apps`, remembering the last scope and duration, re-granting after a restart, a floating Stop panel, a global hotkey, and revoking on screen lock remain open.
- Whether a display filter that excludes MindRoom still renders the Dock, menu bar, and wallpaper, whether status-item windows report MindRoom as their owner on macOS 26, and whether a first launch of an app shows an Automation prompt are checked only by the owner-run release gate in `macos/README.md`.
