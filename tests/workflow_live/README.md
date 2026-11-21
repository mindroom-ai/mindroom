# Live fault scripts for workflow runs

These scripts break a real MindRoom backend in the middle of a workflow run and count what happened.
They are not pytest tests.
They run against a throwaway deployment made by `~/repos/dev/scripts/live.py` (`just live up`), never against lab or production, and the deployment is torn down afterwards.

Both scripts are standard library only and Linux only.
Each prints one result line on stdout and exits 0 when that line is the expected one.

## The scripts

| Script | What it does | Expected line |
|---|---|---|
| `kill_after_write.py --barrier before-receipt` | Kills the backend with `SIGKILL` after the run's one write took effect and before its receipt was saved. | `lines=1 node=indeterminate reports=0` |
| `kill_after_write.py --barrier after-receipt` | Kills the backend with `SIGKILL` after the receipt was saved and before the run's node was written. | `lines=1 node=cached reports=1` |
| `restart_mid_run.py` | Stops the backend with `SIGTERM` after three of six writes and starts it again. | `lines=6 nodes=6/6 attempts=2 reports=1` |

`lines` is the number of lines in the file the run appends to, so a write that ran twice shows as one line too many.
`node` is the state of the write's call; `cached` means a later attempt read the saved result back without running the call.
`reports` is the number of agent replies in the thread that carry the run's result.

`kill_after_write.py` waits for the barrier's own `.reached` file and never for the line in the counter file.
The backend writes that file at the barrier, after the tool's effect, so the kill always lands in the state the barrier names.
The barriers are `workflow_runs.faults.hold`, which does nothing unless `MINDROOM_WORKFLOW_FAULT_DIR` is set.
The script restarts the backend with that variable set, and starts it without the variable after the kill.

In the `before-receipt` case the run fails, because the script does not catch the error of the call that may already have run.
No report is delivered, and `resume_workflow(run_id, rerun=["n1"])` runs the call again on purpose.

## Local stack

The run's shell call and its script process run on this machine, as children of the backend.

```bash
cd ~/repos/dev
just live up --backend ~/repos/worktrees/mindroom-agent-graphs    # prints the slot N
cd ~/repos/worktrees/mindroom-agent-graphs
python3 -m tests.workflow_live.kill_after_write --barrier before-receipt --slot N
python3 -m tests.workflow_live.kill_after_write --barrier after-receipt --slot N
python3 -m tests.workflow_live.restart_mid_run --slot N
cd ~/repos/dev
just live down --slot N
```

`just live up` runs the working tree of `--backend`, so commit or keep the change you want to test in that checkout.
The scripts edit the slot's `config.yaml` once: the Helper gets the `workflow` toolkit, and `defaults.allow_shell_without_worker: true` lets its shell and its scripts run on this machine.
Each script then asks the Helper, through `just live ask`, to start a run from a file it saved in the Helper's workspace.

## Stack with a Docker worker

The run's shell call and its script process run in Docker workers of the backend.
Build the worker image from the commit under test, then point the slot at it before running the scripts.

```bash
cd ~/repos/dev
just live up --backend ~/repos/worktrees/mindroom-agent-graphs    # prints the slot N
SLOT=/tmp/mindroom-client-agents/slot-N
SHA=$(python3 -c "import json; print(json.load(open('$SLOT/state.json'))['sha'][:12])")
git -C ~/repos/worktrees/mindroom-agent-graphs archive "$SHA" \
  | docker build --quiet --tag "mindroom-workflow-live-worker:$SHA" --file local/instances/deploy/Dockerfile.mindroom -
cat >> "$SLOT/.env" <<ENV
MINDROOM_WORKER_BACKEND=docker
MINDROOM_DOCKER_WORKER_IMAGE=mindroom-workflow-live-worker:$SHA
MINDROOM_DOCKER_WORKER_NAME_PREFIX=mindroom-workflow-live-slot-N
MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS=300
MINDROOM_SANDBOX_PROXY_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")
ENV
sed -i 's/^defaults:$/defaults:\n  worker_tools: [shell]/' "$SLOT/config.yaml"
cd ~/repos/worktrees/mindroom-agent-graphs
python3 -m tests.workflow_live.kill_after_write --barrier before-receipt --slot N
python3 -m tests.workflow_live.kill_after_write --barrier after-receipt --slot N
python3 -m tests.workflow_live.restart_mid_run --slot N
docker ps --all --quiet --filter "name=mindroom-workflow-live-slot-N" | xargs --no-run-if-empty docker rm --force
cd ~/repos/dev
just live down --slot N
```

The scripts restart the backend themselves, so the new `.env` lines take effect on their first restart.
With `worker_tools` set, the scripts leave `allow_shell_without_worker` out, and a worker that is down refuses the run instead of falling back to this machine.

## What to check besides the result line

- The stderr line `processes of the killed backend still running` names what the kill left behind after the run ended; it should say `none`.
- `get_workflow(run_id)` in the thread shows the run's attempts and the call's state as the result line reports them.
