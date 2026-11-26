# Live fault scripts for workflow runs

These scripts break a real MindRoom backend in the middle of a workflow run and count what happened.
They are not pytest tests.
They run against a throwaway deployment, never against lab or production, and the deployment is torn down afterwards.

All scripts are standard library only and Linux only.
Each prints one result line on stdout and exits 0 when that line is the expected one.

## The scripts

| Script | What it does | Expected line |
|---|---|---|
| `kill_after_write.py --barrier before-receipt` | Kills the backend with `SIGKILL` after the run's one write took effect and before its receipt was saved. | `lines=1 node=indeterminate attempt=2 run=failed deliveries=1 reports=0` |
| `kill_after_write.py --barrier after-receipt` | Kills the backend with `SIGKILL` after the receipt was saved and before the run's node was written. | `lines=1 node=cached attempt=2 run=completed deliveries=1 reports=1` |
| `restart_mid_run.py` | Stops the backend with `SIGTERM` after three of six writes and starts it again. | `lines=6 nodes=6/6 attempt=2 run=completed deliveries=1 reports=1` |
| `agents_restart.py` | Stops the backend with `SIGTERM` while the second of three agents is working, after that agent wrote its one line, and starts it again. The first two are copies of the Helper (the first with `minimal=True`); the third is the lobby's second agent, `deckhand`. | `lines=1 agents=3/3 subagents=3 attempt=2 run=completed deliveries=1 reports=1` |
| `approval_restart.py` | One agent writes its one line and then calls `write_file`, which asks for an approval. While the card is open the script stops the backend with `SIGTERM`, starts it, kills it with `SIGKILL`, starts it again, and then approves the card. | `lines=1 cards=1 open=1 saved=1 agents=1/1 attempt=3 run=completed deliveries=1 reports=1` |
| `diamond_restart.py` | A plan agent returns six lines, `foreach` runs one research agent per line, two at a time, and one agent joins their answers. The script stops the backend with `SIGTERM` after three research agents completed and starts it again. | `agents=8/8 subagents=8 paid_once=True attempt_below_run=True attempt=2 run=completed deliveries=1 reports=1` |

| `pauses.py` | Two runs. The first has an agent with a `schema` that no value matches, which fails with kind `schema` after its five turns. The second has `max_cost_usd=0.000001`: an `ask_user` that the script answers `Yes` after it stopped the backend with `SIGTERM` and started it again, one agent that spends past the limit, and a second agent that never starts as the run ends `interrupted`. | `schema_turns=5 schema_kind=schema questions=1 answered=Yes held=1 limit_reason=True attempt=2 run=interrupted` |
| `graph_frames.py` | Has the backend make the lobby an encrypted room and runs one `foreach` over 200 agents that answer with one word. The backend keeps each graph frame and each post and edit of the run's card in a capture directory, and the script reads the room's job state while the run goes on. | `agents=200/200 encrypted=True refused=0 rate_limited=0 state_jobs=1 state_kinds=workflow state_labels=0 card=completed graph=True saved=4 run=completed` |

- `lines` is the number of lines in the file the run appends to, so a write that ran twice shows as one line too many.
- `node` is the state of the write's call; `cached` means a later attempt read the saved result back without running the call.
- `agents` counts the `agent()` calls that completed as the call they first were, so an agent that was started a second time shows as one too few; `subagents` is the number of subagents those calls ran as.
- `attempt=2` shows that the run was replayed after the kill; a run that only ended `interrupted` would show `attempt=1`.
  `approval_restart.py` restarts twice, so it shows `attempt=3`.
- `cards` is the number of approval cards the agent's `write_file` call got, and `open` how many of them were still open after both restarts; a card that a restart closed and asked again shows as `cards=2`.
  `saved` is the number of lines in the file the approved call wrote, so an approval that was not accepted shows as `saved=0`.
- `paid_once` says that the run's usage ledger pays no agent twice.
  Each agent call has one entry for the turn it ended with.
  A call that had completed before the restart has no other entry, and its entry is of the first attempt.
  A call whose turn the restart cut short may have one more entry, of an earlier attempt, for what that turn had spent by then.
  How many calls are cut short, none to two, depends on when the restart lands, so the number of entries is printed on stderr and is not part of the line.
  `attempt_below_run` says that the attempt after the restart used less than the whole run, in USD when every turn has a price and in tokens otherwise.
- `schema_turns` is the number of turns the agent with a `schema` took and `schema_kind` the kind of the error its call ended with, `questions` is the number of cards the lobby holds for the script's question without their edits, `held` is 1 when the second agent never started a job (either never submitted or `cancelled` without a `job_id`), and `limit_reason` is `True` when the run ended with `Stopped at the cost limit (...)`.
- `refused` counts the graph frames the homeserver did not accept, and `rate_limited` the `M_LIMIT_EXCEEDED` errors in the backend's log since the script started it.
  `state_jobs` is the most jobs the room's `io.mindroom.background_jobs` state listed at once, `state_kinds` their kinds, and `state_labels` how many of them carried an agent's label.
  `saved` is the number of files `graph_frames.py` wrote to the slot's log directory: `workflow_frame_first_snapshot.json`, `workflow_frame_first_delta.json`, `workflow_frame_last_snapshot.json` and `workflow_card_final.json`.
  They are what MindRoom Chat's fixtures are made from.
- `run` is the state the run ended in.
- `deliveries` is the number of follow-up turns the journal admitted for the run's job, read from `journal_events` in the slot's `storage/tracking/event_journal.db`.
  It counts the delivery itself, whatever the model then wrote.
- `reports` is the number of agent replies in the thread that carry the run's result, so it counts successful reports only.

`kill_after_write.py` waits for the barrier's own `.reached` file and never for the line in the counter file.
The backend writes that file at the barrier, after the tool's effect, so the kill always lands in the state the barrier names.
The barriers are `workflow_runs.faults.hold`, which does nothing unless `MINDROOM_WORKFLOW_FAULT_DIR` is set.
The script restarts the backend with that variable set, and starts it without the variable after the kill.

In the `before-receipt` case the second attempt fails, because the script does not catch the error of the call that may already have run.
That failure is delivered once, no report is, and `resume_workflow(run_id, rerun=["n1"])` runs the call again on purpose.

Each script edits the slot's `config.yaml`: the Helper gets the `workflow` toolkit, and `defaults.allow_shell_without_worker: true` unless the config routes tools to workers.
Every edit goes through `Deployment.edit_config`, and the script's exit writes the file back as the script found it, so no script meets the edit of another.
A script that is killed with `SIGKILL` leaves its edit behind.
`agents_restart.py` also adds the agent `deckhand` to the lobby, which the Helper may not delegate to: a run runs it because its bot is in the room, and prepends an `auto_approve` rule for `run_shell_command` so the minimal agent's shell is not excluded by the stack's scripted shell-review rule.
`approval_restart.py` also gives the Helper the `filesystem` toolkit and adds a `tool_approval` rule, first in the list of rules, that makes `write_file` ask for an approval, and approves the card itself with an `io.mindroom.tool_approval_response` event of the test account.
A script names a toolkit by its registered name: `tool("file", ...)` fails with `The requested tool is no longer available to this script run.` since `file` became another name of `filesystem`, and an approval rule that matches the retired `save_file` alone stops the backend from starting.
A running backend reloads a config edit some time later, so each script starts the backend again after an edit that changed the file, and only then sends its message.
`agents_restart.py` also waits until the bot of `deckhand` has joined the lobby.
`graph_frames.py` sets `room_defaults.encrypted: true`, with which the backend encrypts the lobby when it starts: the test account has no right to change that state itself.
The room stays encrypted when the config is written back, since nothing undoes that.
The script starts the backend with `MINDROOM_WORKFLOW_FAULT_DIR` set to its capture directory: `workflow_runs.faults.capture` writes `workflow_frames.jsonl` and `workflow_cards.jsonl` there, and nothing without that variable.
Its one message reaches the Helper either way: `--send ask` types it in Chat, which encrypts it, and `--send api` sends it as it is, which the Helper also takes in an encrypted room.
What the run shows the room is encrypted in both cases.
`pauses.py` reads the question cards from the lobby and answers them with `io.mindroom.workflow_answer` events of the test account, so it needs a lobby that is not encrypted: run it before `graph_frames.py`, or on another slot.
It also needs a model with a price, since a run on an unpriced model never reaches a cost limit; without one it says so and exits 1.
Each script then asks the Helper to start a run from a file it saved in the Helper's workspace.
It sends that one message with `just live ask`, which types it in Chat with Chromium.
Pass `--send api` to send it through the homeserver's client API instead, on a machine with no browser, when Chat does not load, or for a stack without Chat.

## Local stack

The run's shell call and its script process run on this machine, as children of the backend.
`just live up` needs a MindRoom Chat checkout whose `scripts/e2e-slot.sh` has `up`; pass one with `--chat DIR` when the submodule of `~/repos/dev` is older.

```bash
cd ~/repos/dev
just live up --backend ~/repos/worktrees/mindroom-agent-graphs    # prints the slot N
cd ~/repos/worktrees/mindroom-agent-graphs
python3 -m tests.workflow_live.kill_after_write --barrier before-receipt --slot N
python3 -m tests.workflow_live.kill_after_write --barrier after-receipt --slot N
python3 -m tests.workflow_live.restart_mid_run --slot N
python3 -m tests.workflow_live.agents_restart --slot N
python3 -m tests.workflow_live.approval_restart --slot N
python3 -m tests.workflow_live.diamond_restart --slot N
python3 -m tests.workflow_live.pauses --slot N
python3 -m tests.workflow_live.graph_frames --slot N          # last: it encrypts the lobby
cd ~/repos/dev
just live down --slot N
```

`just live up` runs the working tree of `--backend`, so commit or keep the change you want to test in that checkout.

## Stack with a Docker worker

The run's shell call and its script process run in Docker workers of the backend.
`just live up` has no option for workers, so start the slot's homeserver and the agent stack by hand.
`CHAT` is a MindRoom Chat checkout with `scripts/e2e-slot.sh up` and `scripts/agent-stack/stack.py`, for example a worktree of its `origin/main`.
`--computers --computer-tools shell` builds the worker image `mindroom-client-e2e-worker:<commit>` from the commit with `local/instances/deploy/Dockerfile.mindroom` (a few minutes the first time), installs MindRoom's `docker` extra, sets `MINDROOM_WORKER_BACKEND=docker`, and routes `shell` to the workers.
The stack has no Chat of its own, so every script takes `--send api`.

```bash
CHAT=~/repos/dev/mindroom-chat                                      # or a worktree of its origin/main
N=41                                                                # a free slot of 1-49: no container mindroom-client-e2e-41-* in `docker ps`
cd "$CHAT"
bash scripts/e2e-slot.sh "$N" up
export OPENAI_API_KEY="$(sed -n 's/^\(export \)\{0,1\}OPENAI_API_KEY=//p' ~/.mindroom-chat/.env | head -1)"
python3 scripts/agent-stack/stack.py "$N" up --model live --client-sessions \
  --computers --computer-tools shell \
  --checkout ~/repos/worktrees/mindroom-agent-graphs --ref HEAD
# The workers reach the backend's script gateway and minimal-agent CLI API over the Docker bridge; the stack's backend listens on loopback only.
API=$((29600 + N))
echo "MINDROOM_SCRIPT_GATEWAY_URL=http://172.17.0.1:$API/api/script-gateway" >> "/tmp/mindroom-client-agents/slot-$N/.env"
echo "MINDROOM_AGENT_CLI_PRIMARY_URL=http://172.17.0.1:$API" >> "/tmp/mindroom-client-agents/slot-$N/.env"
nix shell nixpkgs#socat --command socat TCP-LISTEN:$API,bind=172.17.0.1,fork,reuseaddr TCP:127.0.0.1:$API &
sudo nft add element inet nixos-fw temp-ports "{ tcp . $API }"     # NixOS firewall; drop it again after the teardown
cd ~/repos/worktrees/mindroom-agent-graphs
python3 -m tests.workflow_live.kill_after_write --barrier before-receipt --slot "$N" --send api
python3 -m tests.workflow_live.kill_after_write --barrier after-receipt --slot "$N" --send api
python3 -m tests.workflow_live.restart_mid_run --slot "$N" --send api
python3 -m tests.workflow_live.agents_restart --slot "$N" --send api
python3 -m tests.workflow_live.approval_restart --slot "$N" --send api
python3 -m tests.workflow_live.diamond_restart --slot "$N" --send api
docker ps --all --filter "label=mindroom-client-e2e.slot=$N"        # the slot's workers, before the teardown
cd "$CHAT"
bash scripts/e2e-slot.sh "$N" down                                  # stops the agent stack, its workers and the homeserver
```

The scripts restart the backend themselves and find the slot through the stack's own `state.json` in `/tmp/mindroom-client-agents/slot-N`.
With `worker_tools` set, the scripts leave `allow_shell_without_worker` out, and a worker that is down refuses the run instead of falling back to this machine.
`docker ps` must list a worker of the slot while a run is going on; if it lists none, the run did not use a worker and the result says nothing about Docker.
Minimal agents in Docker workers (`agents_restart.py`'s `first` agent) also require `MINDROOM_API_KEY` (which `stack.py` already writes to the slot's `.env`) and `MINDROOM_AGENT_CLI_PRIMARY_URL` unless `MINDROOM_SCRIPT_GATEWAY_ISOLATED` and `MINDROOM_SCRIPT_GATEWAY_PORT` are set.

## What has been run

- 2026-11-20 at `d2525db67`, local stack (slot 50) and Docker stack (slot 43): all four scripts printed their expected lines.
- `agents_restart.py` was added later.
  Its first live run failed before the run started: the Helper got the message before the backend had reloaded the config edit, so it had no `workflow` toolkit yet.
  The script now starts the backend again after the edit, and its third call runs a room agent.
  It has not been run since that change.
- `approval_restart.py` and `diamond_restart.py` were added with the approval restore, the fan-out and the usage ledger.
  Their first live runs failed in the scripts themselves.
  `approval_restart.py` refused a config whose `tool_approval` section already had rules, after it had given the Helper its toolkits and before it started the backend again.
  `diamond_restart.py` ran next, found the toolkits in the config, and so asked the Helper three seconds before the running backend reloaded that edit: the Helper had no `workflow` tool yet and started nothing.
  It would not have found a run either way, because it looks for a run by a word that its script did not hold.
- 2026-11-22 at `1beaf6aef`, local stack (slot 50), with both scripts fixed: both printed their expected lines.
  The diamond run cost 0.0017 USD in all, and 0.0005 USD in the attempt after the restart.
- Without the gateway lines above, the Docker stack refuses every run with `Background-script workers require MINDROOM_SCRIPT_GATEWAY_URL or MINDROOM_PUBLIC_URL.`: the stack's `MINDROOM_PUBLIC_URL` is a loopback address, which workers may not use.

- `pauses.py` and `graph_frames.py` were added with the run card, the graph frames, `schema`, `ask_user` and `max_cost_usd`.
  Neither has been run.

## What to check besides the result line

- The stderr line `processes of the killed backend still running` names what the kill left behind after the run ended; it should say `none`.
- After `agents_restart.py`, the Sub-agents panel of the thread lists three agents, `first`, `second` and `third`, and none of them twice.
  The third call starts after the restart, when the bot of `deckhand` may not have synced yet: the call must then wait and run, not fail with `refused`.
- `get_workflow(run_id)` in the thread shows the run's attempts and the call's state as the result line reports them.
