"""Restart a live backend while an agent of a workflow run is working, and count what happened.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    python3 -m tests.workflow_live.agents_restart [--slot N]

The run calls `agent()` three times, one after the other: two copies of the Helper (the first in minimal mode),
then the lobby's second agent.
The backend is started again after the config edit, so the Helper has the toolkit before it is asked.
The second agent appends one line to a file with its shell tool and then sleeps, so it is still working when the
script stops the backend with SIGTERM and starts it again.
The third call starts after that restart, when the second agent's own bot may not have synced yet.
When the run has ended the script prints

    lines=<lines in the file> agents=<completed agent calls>/<agent calls> subagents=<subagents the calls ran as>
    attempt=<attempts of the run> run=<how the run ended> deliveries=<follow-up turns that delivered the run's job>
    reports=<replies with its result>

Expected: `lines=1 agents=3/3 subagents=3 attempt=2 run=completed deliveries=1 reports=1`.
It exits 0 when the line was written once, each call ran as one subagent that was started once, the run completed on
a second attempt, and one reply carries its result.
Standard library only; Linux only.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time

from tests.workflow_live.kill_after_write import (
    ROOM_AGENT,
    Deployment,
    LiveError,
    add_send_options,
    counter_lines,
    outcome,
    say,
    start_run,
    until,
)

_THREE_AGENTS = """\
from mindroom.workflow import agent


def main(args):
    agent("Reply with the single word: one", name="first", minimal=True)
    agent(
        "Run the shell command `echo written >> {counter}` exactly once. Then run the shell command `sleep {sleep}`. "
        "Then reply with the single word: two. Never run the echo command a second time, whatever happens.",
        name="second",
    )
    agent("Reply with the single word: three", name="third", agent="{room_agent}")
    return {{"report": "{report}"}}
"""
_EXPECTED = "lines=1 agents=3/3 subagents=3 attempt=2 run=completed deliveries=1 reports=1"


def main() -> int:
    """Run the restart once and print its result line; exit 0 when it is the expected one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--sleep", type=int, default=45, help="seconds the second agent sleeps after its write")
    parser.add_argument("--slot", type=int, help="the live deployment's slot (default: the only ready one)")
    add_send_options(parser)
    parser.add_argument("--settle", type=float, default=90, help="seconds to wait for reports after the run ended")
    args = parser.parse_args()
    dev = args.dev if args.send == "ask" else None
    deployment = Deployment.find(args.slot)
    token = f"tide{secrets.token_hex(6)}"
    counter = f"counter_{token}.txt"
    # Only the run's result holds this word: no prompt and no agent's answer does, so no other reply repeats it.
    report = f"harbor{secrets.token_hex(6)}"

    backend = deployment.backend()
    # A running backend reloads the edit some time later; one started after it has the toolkit before it is asked.
    if deployment.give_workflow_toolkit(room_agent=True) or "MINDROOM_WORKFLOW_FAULT_DIR" in backend.environment:
        backend.stop()
        backend.start()
    until(f"the bot of {ROOM_AGENT} in the lobby", lambda: deployment.in_lobby(ROOM_AGENT), 240, 1)
    source = _THREE_AGENTS.format(counter=counter, sleep=args.sleep, report=report, room_agent=ROOM_AGENT)
    root = start_run(deployment, source, token, dev)

    until("the second agent's line", lambda: counter_lines(deployment, counter) or None, 600)
    say(f"the second agent wrote its line; restarting the backend (process {backend.pid})")
    backend.stop()
    backend.start()
    say("the backend is back; waiting for the run to end")
    run, nodes = deployment.ended_run(token, 900)
    time.sleep(args.settle)
    agents = [node for node in nodes if node["kind"] == "agent"]
    completed = sum(node["state"] == "completed" and node["generation"] == 1 for node in agents)
    subagents = {json.loads(node["links_json"]).get("subagent_id") for node in agents}
    line = (
        f"lines={counter_lines(deployment, counter)} agents={completed}/{len(agents)} subagents={len(subagents)} "
        f"{outcome(deployment, run)} reports={deployment.replies_with(root, report)}"
    )
    print(line, flush=True)
    return 0 if line == _EXPECTED else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
