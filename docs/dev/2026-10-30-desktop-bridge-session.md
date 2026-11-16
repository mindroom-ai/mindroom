# One Desktop bridge session for the terminal and the macOS helper

This record keeps the decisions behind the October 2026 Desktop session refactor that the code cannot show: why the run lifecycle has one owner, the ordering it must keep, the alternatives design review rejected, and the limits that remain.

## One session owner, two supervisors

`mindroom desktop run` and the macOS app helper used to open Matrix, build the bridge, admit commands, verify the controller, start the workers, and tear everything down in two copies that had already drifted apart.
`desktop/bridge_session.py` now owns that sequence for both, and each caller keeps only what differs: the terminal requests GUI permissions, announces the run, serves shell approvals in the terminal, and exits when the first worker ends; the helper records a fault for the app's status and serves the app's local controls.

Startup releases what it acquired in reverse order through one exit stack, so a failure at any step, including a cancelled start, leaves nothing open.
A run that never started its workers is not stopped: stopping gives recorded replies a final delivery, and that must not happen before the pinned controller's identity was checked.

Closing keeps the order the resources depend on.
The first close starts its teardown eagerly, so admission, new execution, local control, and shell access end before the close first yields, as a direct stop did before: a command admitted just before a stop never starts, and a shell command whose controller check finishes as the stop begins is refused instead of started and killed.
Native input runs in threads that cancelling a task cannot stop, so the bridge stops first (ends shell access and input, waits a bounded time for the active action, delivers recorded replies), and only after that are the workers cancelled and the journal, folders, browser, and Matrix owner released, with the durable source lease released before its HTTP connection.
The terminal stops reading shell approvals before the bridge stops, because stopping settles a pending approval.
Concurrent closes share one teardown that a cancelled caller cannot cut short, which is what lets the helper's stop wait for a close its failed-worker supervisor already began.
Every release is attempted even when an earlier one fails, and that failure is the close's outcome; the helper still retires the runtime, so the next start is possible once the failed resource is free.

The helper shows the live bridge status until the failed bridge has drained, so the app keeps showing the action that is still finishing, and only then reports the fault.

## A worker that returns is a failure

The bridge workers, the durable transport, and the terminal approver never return while their session is open, so a normal return means a worker stopped serving.
The helper already reported it as a fault; the terminal used to exit cleanly with status 0, which would hide the stop.
Both now treat it as a failure, and the terminal exits with `Desktop bridge failed: A desktop worker stopped unexpectedly.`

## Matrix request headers

Each Desktop command passes its own homeserver and Access choice to one header helper, so the Access token is always issued for the homeserver that command talks to.
The Matrix client prepares the Access token before every request, so no command prepares it up front; with an explicit SSO login the Access sign-in therefore comes after the browser SSO step.

## Rejected alternatives

- Keeping the bridge builder separate and handing its providers to the session: provider closing would again have two owners, the builder on failure and the session after success.
- Stopping any bridge that was built, as the terminal used to: it delivered recorded replies before the controller check passed.
- A two-step start, opening first and starting workers separately, so the terminal could grant shell auto-approval in between: the run-only control lease and shell grant are start parameters instead, next to each other.
- Supervision inside the session: the terminal also waits on its approver and the helper records a fault, so a free function that reports the first worker's exit serves both.
- Logging release failures instead of returning them: the app reads only the helper's protocol, not its logs, so a failed release must reach it as the stop's error.

## Ending input that is already running

Native input runs in a worker thread that no task cancellation reaches, so revoking control and stopping close the provider's input gate instead, which every pointer event, key, text chunk, app launch, and accessibility press or value write checks right before it starts, after any slower check of the target.
A button or key that input already pressed is still released, and an event already handed to the system, such as a key chord posted in one call or a whole Linux or Windows pointer motion, still completes; focusing the target app before its input is not gated, because observations focus apps too.
A reusable gate alone would let a quick new grant reopen it under the old action, so a grant is refused while input interrupted by a revoke is still inside its native call; the follow-up observation after that call does not hold the grant back.
Each grant that starts control anew begins a new authority, and a control command runs only under the authority it arrived in, so commands queued before a revoke, an expiry, or a restart never run under a later grant; that admission is kept in memory because the Matrix command carries no grant identity, and extending a grant that is still in force keeps its queued commands.

Stop waits at most 5 seconds for the running action, because a hung application can hold a native call indefinitely.
The gate is closed first, so the abandoned call sends no further input, and the workers are cancelled before the journal closes, so its late return records nothing; its command stays started and is reported as interrupted on the next start.
Stop's bound is not the helper's exit bound: Python waits for its worker threads when the process exits, so the helper exits only once that native call returns.
The bound also covers folder reads on the same lane, so a read stuck past it, such as on an unresponsive network volume, is abandoned while its folder descriptors close; its result is discarded.

## Bounded accessibility reads

The helper sets a process-wide accessibility messaging timeout of 2 seconds, and one tree read checks its 5-second deadline before each message, so a read of an app that stops answering ends within about 7 seconds, not 5.
`get_app_state` stops re-reading for a stable tree once 8 seconds have passed and returns the latest tree as unstable, so its tree takes up to about 15 seconds from a slow app that still answers.
Its default screenshot then re-reads the tree up to twice more and captures within 5 seconds, so one such command can outlast the default 30-second command timeout; a hung app instead fails its first read within about 7 seconds.
A `primary-screen` screenshot never reads an app's accessibility tree, so it works as soon as the lane is free.
A second lane for screen observations was rejected: the accessibility state store would have to become thread-safe, and a capture could run during input, for a wait the deadlines already bound.
These limits were chosen below the default 30-second command timeout without measurements on heavy trees, so a tree that legitimately takes longer than the deadline to read now fails.

## Known limits

A second cancellation during startup rollback interrupts only the release it lands in; the exit stack still runs the remaining releases before the startup error is raised.
That needs a host stop and the app closing the helper's input during the same startup, after which the helper process exits.
