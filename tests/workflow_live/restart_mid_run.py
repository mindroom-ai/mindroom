"""Restart a live backend the graceful way in the middle of a workflow run, and count what happened.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    python3 -m tests.workflow_live.restart_mid_run [--writes 6] [--after 3] [--slot N]

The run appends `--writes` lines to a file, one call each, and sleeps in Python between them, so no call is in flight
most of the time.
When `--after` calls have completed the script stops the backend with SIGTERM, starts it again, waits for the run to
end, and prints

    lines=<lines in the file> nodes=<completed calls>/<calls> attempts=<attempts of the run> reports=<replies with its result>

Expected with the defaults: `lines=6 nodes=6/6 attempts=2 reports=1`.
It exits 0 when every write happened once, the run completed on a second attempt, and one reply carries its result.
Standard library only; Linux only.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

from tests.workflow_live.kill_after_write import Deployment, LiveError, counter_lines, say, start_run, until

_WRITE_MANY = """\
import time

from mindroom.workflow import shell


def main(args):
    for index in range({writes}):
        shell(f"echo {{index}} >> {counter}")
        time.sleep(4)
    return {{"report": "{token}"}}
"""


def main() -> int:
    """Run the restart once and print its result line; exit 0 when it is the expected one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--writes", type=int, default=6, help="how many lines the run appends")
    parser.add_argument("--after", type=int, default=3, help="restart once this many calls have completed")
    parser.add_argument("--slot", type=int, help="the live deployment's slot (default: the only ready one)")
    parser.add_argument("--dev", type=Path, default=Path("~/repos/dev").expanduser(), help="where `just live` runs")
    parser.add_argument("--settle", type=float, default=90, help="seconds to wait for reports after the run ended")
    args = parser.parse_args()
    deployment = Deployment.find(args.slot)
    token = f"tide{secrets.token_hex(6)}"
    counter = f"counter_{token}.txt"

    backend = deployment.backend()
    deployment.give_workflow_toolkit()
    if "MINDROOM_WORKFLOW_FAULT_DIR" in backend.environment:
        backend.stop()
        backend.start()
    root = start_run(deployment, _WRITE_MANY.format(writes=args.writes, counter=counter, token=token), token, args.dev)

    def enough_done() -> bool:
        found = deployment.run(token)
        return found is not None and sum(node["state"] == "completed" for node in found[1]) >= args.after

    until(f"{args.after} completed calls", enough_done, 600)
    say(f"{args.after} calls completed; restarting the backend (process {backend.pid})")
    backend.stop()
    backend.start()
    say("the backend is back; waiting for the run to end")
    run, nodes = deployment.ended_run(token, 600)
    time.sleep(args.settle)
    completed = sum(node["state"] == "completed" for node in nodes)
    line = (
        f"lines={counter_lines(deployment, counter)} nodes={completed}/{len(nodes)} "
        f"attempts={run['attempt']} reports={deployment.replies_with(root, token)}"
    )
    print(line, flush=True)
    say(f"run {run['run_id']} ended {run['state']}")
    return 0 if line == f"lines={args.writes} nodes={args.writes}/{args.writes} attempts=2 reports=1" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
