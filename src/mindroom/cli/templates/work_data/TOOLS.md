# TOOLS.md

Mechanics of this setup.

## Connected accounts

Connections the person makes on this private agent are their own, so anything done through one is done as them.
A deployment can also give a tool a shared account, so check whose account a tool uses before saying whose data it reached.
List each service here when the person connects it, and remove it when they disconnect.

| Service | What it reaches |
| --- | --- |

## Workers and the workspace

The workspace is this person's durable place, however the deployment runs tools.
`shell` and `file` start in it, so relative paths land there, and `$MINDROOM_AGENT_WORKSPACE` holds its absolute path.
When tools run in a worker, `~` points into the workspace too, and MindRoom may stop or recreate that worker.
Anything outside the workspace there, such as `/tmp`, the worker's Python environment, and running or background shells, can vanish with it.
When tools run in MindRoom's own process instead, nothing is recreated, but `~` is the host account's home, not the workspace.
There, `shell` can also reach other people's copies of this workspace, so never promise the person that no one else's tools can read their notes.
Install extra command-line tools at `.local/bin` inside the workspace.
In a worker, add `export PATH="$MINDROOM_AGENT_WORKSPACE/.local/bin:$PATH"` to `.mindroom/worker-env.sh`, which runs before each worker shell or Python call.
Without a worker that hook never runs, so call such a tool by its relative path, `.local/bin/<tool>`.

## Skills

Workspace skills live in `skills/<name>/SKILL.md` and load on the next run.
Platform-wide skills are read-only from here, and a workspace skill with the same name would replace the shared one for this person, so a personal variant takes a distinct name.

## Replies

Answer in the current thread as usual, because the reply is delivered there.
Reserve the Matrix message tool for Matrix actions the person requested, such as posting in another room, reacting, or editing a message.

## Agreed reminders

`list_schedules` shows what is live, and this table records what the person agreed to.

| Task ID | Covers | Cadence | Silent |
| --- | --- | --- | --- |
