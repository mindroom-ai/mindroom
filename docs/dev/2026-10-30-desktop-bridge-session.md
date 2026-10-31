# One Desktop bridge session for the terminal and the macOS helper

This record keeps the decisions behind the October 2026 Desktop session refactor that the code cannot show: why the run lifecycle has one owner, the ordering it must keep, the alternatives design review rejected, and the limits that remain.

## One session owner, two supervisors

`mindroom desktop run` and the macOS app helper used to open Matrix, build the bridge, admit commands, verify the controller, start the workers, and tear everything down in two copies that had already drifted apart.
`desktop/bridge_session.py` now owns that sequence for both, and each caller keeps only what differs: the terminal requests GUI permissions, announces the run, serves shell approvals in the terminal, and exits when the first worker ends; the helper records a fault for the app's status and serves the app's local controls.

Startup releases what it acquired in reverse order through one exit stack, so a failure at any step, including a cancelled start, leaves nothing open.
A run that never started its workers is not stopped: stopping gives recorded replies a final delivery, and that must not happen before the pinned controller's identity was checked.

Closing keeps the order the resources depend on.
The first close starts its teardown eagerly, so admission, new execution, local control, and shell access end before the close first yields, as a direct stop did before: a command admitted just before a stop never starts, and a shell command whose controller check finishes as the stop begins is refused instead of started and killed.
Native input runs in threads that cancelling a task cannot stop, so the bridge stops first (ends shell access, waits for the active action, delivers recorded replies), and only after that are the workers cancelled and the journal, folders, browser, and Matrix owner released, with the durable source lease released before its HTTP connection.
The terminal stops reading shell approvals before the bridge stops, because stopping settles a pending approval.
Concurrent closes share one teardown that a cancelled caller cannot cut short, which is what lets the helper's stop wait for a close its failed-worker supervisor already began.
Every release is attempted even when an earlier one fails, and that failure is the close's outcome; the helper still retires the runtime, so the next start is possible once the failed resource is free.

The helper shows the live bridge status until the failed bridge has drained, so the app keeps showing the action that is still finishing, and only then reports the fault.

## A worker that returns is a failure

The bridge workers, the durable transport, and the terminal approver never return while their session is open, so a normal return means a worker stopped serving.
The helper already reported it as a fault; the terminal used to exit cleanly with status 0, which would hide the stop.
Both now treat it as a failure, and the terminal exits with `Desktop bridge failed: A desktop worker stopped unexpectedly.`

## Matrix request headers

Every Desktop command builds its Matrix headers with one helper from three explicit inputs: the homeserver, the headers file, and whether Cloudflare Access is used for this run.
Login passes the homeserver it is about to sign in to; pairing and runs pass the saved session's homeserver and combine an explicit Access choice with the saved one.
The Matrix client prepares the Access token before every request, so no command prepares it up front; with an explicit SSO login the Access sign-in therefore comes after the browser SSO step.

## Rejected alternatives

- Keeping the bridge builder separate and handing its providers to the session: provider closing would again have two owners, the builder on failure and the session after success.
- Stopping any bridge that was built, as the terminal used to: it delivered recorded replies before the controller check passed.
- A two-step start, opening first and starting workers separately, so the terminal could grant shell auto-approval in between: the run-only control lease and shell grant are start parameters instead, next to each other.
- Supervision inside the session: the terminal also waits on its approver and the helper records a fault, so a free function that reports the first worker's exit serves both.
- Logging release failures instead of returning them: the app reads only the helper's protocol, not its logs, so a failed release must reach it as the stop's error.

## Known limits

A second cancellation during startup rollback interrupts only the release it lands in; the exit stack still runs the remaining releases before the startup error is raised.
That needs a host stop and the app closing the helper's input during the same startup, after which the helper process exits.
