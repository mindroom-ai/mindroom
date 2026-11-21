"""Run a workflow whose one call returns a result over the inline limit, and check the script read it back whole.

Usage, from the checkout, on a live deployment (README.md has every step; the plan asks for it on the Docker stack):

    python3 -m tests.workflow_live.large_result [--slot N] [--send api]

The script saves a file of 9 MiB of one character in the Helper's workspace and starts a run that reads it with
`tool("file", "read_file", ...)`, which returns up to 10,000,000 characters.
The result is over the 8 MiB a node keeps in its row, so the engine saves it in a file and the script fetches it.
It prints

    chars=<characters the script got> same=<whether they are the file's> spilled=<whether the node kept a file>
    attempt=<attempts of the run> run=<how the run ended>

Expected: `chars=9437184 same=True spilled=True attempt=1 run=completed`.
It exits 0 when the line it printed is the expected one.
Standard library only; Linux only.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

from tests.workflow_live.kill_after_write import Deployment, LiveError, add_send_options, say, start_run

_CHARS = 9 * 1024 * 1024
_READ_LARGE = """\
from mindroom.workflow import tool


def main(args):
    text = tool("file", "read_file", file_name="{name}")
    return {{"report": "{report}", "chars": len(text), "same": text == "x" * {chars}}}
"""


def main() -> int:
    """Run the read once and print its result line; exit 0 when it is the expected one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--slot", type=int, help="the live deployment's slot (default: the only ready one)")
    add_send_options(parser)
    args = parser.parse_args()
    deployment = Deployment.find(args.slot)
    token = f"tide{secrets.token_hex(6)}"
    name = f"large_{token}.txt"

    backend = deployment.backend()
    deployment.give_workflow_toolkit()
    # The Helper gets the `file` toolkit with the first restart after the edit, which also drops a fault directory.
    backend.stop()
    backend.start()
    deployment.workspace.mkdir(parents=True, exist_ok=True)
    (deployment.workspace / name).write_text("x" * _CHARS)
    source = _READ_LARGE.format(name=name, report=f"harbor{secrets.token_hex(6)}", chars=_CHARS)
    start_run(deployment, source, token, args.dev if args.send == "ask" else None)
    say("the run started; waiting for it to end")
    run, nodes = deployment.ended_run(token, 600)
    result = json.loads(run["result_json"] or "null") or {}
    spilled = bool(nodes) and nodes[0]["result_file"] is not None
    line = (
        f"chars={result.get('chars')} same={result.get('same')} spilled={spilled} "
        f"attempt={run['attempt']} run={run['state']}"
    )
    print(line, flush=True)
    if run["state"] != "completed":
        say(f"run {run['run_id']} ended {run['state']}: {run['error']}")
    return 0 if line == f"chars={_CHARS} same=True spilled=True attempt=1 run=completed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
