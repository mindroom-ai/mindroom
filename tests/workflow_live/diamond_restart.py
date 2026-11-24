"""Restart a live backend in the middle of a fan-out of agents, and count what was run and paid for.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    python3 -m tests.workflow_live.diamond_restart [--after 3] [--sleep 20] [--slot N]

The run is a diamond in plain text: one agent returns six lines, the script splits them, `foreach` runs one research
agent per line, two at a time, and one agent joins their answers.
Each research agent sleeps in its shell tool first, so some are still working when the script stops the backend with
SIGTERM, once `--after` of them have completed, and starts it again.
When the run has ended the script prints

    agents=<completed agent calls>/<agent calls> subagents=<subagents the calls ran as>
    paid_once=<whether no agent was paid for twice> attempt_below_run=<whether the last attempt used less than the run>
    attempt=<attempts of the run> run=<how the run ended> deliveries=<follow-up turns that delivered the run's job>
    reports=<replies with its result>

Expected: `agents=8/8 subagents=8 paid_once=True attempt_below_run=True attempt=2 run=completed deliveries=1 reports=1`.
It exits 0 when each of the eight calls completed as the call it first was and was paid for once, the attempt after
the restart used less than the whole run, and one reply carries the run's result.
`paid_once` reads the run's usage ledger, which has one entry for the turn each agent ended with, and one more for
what a turn spent before the restart cut it short (`_paid_twice`); how many agents were cut short depends on timing.
`attempt_below_run` compares cost when every turn has a price, and tokens otherwise.
Standard library only; Linux only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import secrets
import sqlite3
import sys
import time
from typing import Any

from tests.workflow_live.kill_after_write import (
    Deployment,
    LiveError,
    add_send_options,
    outcome,
    say,
    start_run,
    until,
)

_TOPICS = ("tides", "harbors", "lighthouses", "currents", "anchors", "buoys")
_DIAMOND = """\
# {token}: the word this script's run is found by.
from mindroom.workflow import agent, foreach, phase


def main(args):
    phase("Plan")
    plan = agent("Reply with exactly these six lines and nothing else:\\n{topics}", name="plan")
    topics = [line.strip() for line in plan.splitlines() if line.strip()]
    if len(topics) != 6:
        raise ValueError(f"The plan has {{len(topics)}} lines, not 6: {{plan!r}}")
    phase("Research")
    notes = foreach(
        topics,
        lambda topic: agent(
            f"Run the shell command `sleep {sleep}` exactly once. Then reply with one short sentence about {{topic}}.",
            name=topic,
        ),
        max_concurrent=2,
    )
    phase("Write")
    agent("Join these sentences into one paragraph:\\n\\n" + "\\n".join(notes), name="writer")
    return {{"report": "{report}"}}
"""
_EXPECTED = (
    "agents=8/8 subagents=8 paid_once=True attempt_below_run=True attempt=2 run=completed deliveries=1 reports=1"
)
# The ledger names what a turn spent before a restart cut it short `cut:<the run of the agent that was cut short>`.
_CUT_SHORT = "cut:"


def _ledger(deployment: Deployment, run_id: str) -> list[tuple[str, str, int, dict[str, Any]]]:
    """Return the call, the turn, the attempt and the usage of each entry of the run's ledger."""
    [path] = sorted((deployment.directory / "storage").glob("**/workflow_runs/workflow_runs.sqlite3"))
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as connection:
        query = "SELECT key, turn, attempt, usage_json FROM workflow_usage WHERE run_id = ?"
        rows = connection.execute(query, (run_id,))
        return [(str(key), str(turn), int(attempt), json.loads(usage)) for key, turn, attempt, usage in rows]


def _paid_twice(
    ledger: list[tuple[str, str, int, dict[str, Any]]],
    agents: list[str],
    finished: set[str],
) -> list[str]:
    """Return what the ledger holds that pays an agent twice, one sentence per finding; *agents* are the calls' keys.

    Every agent has one entry for the turn it ended with. One that had *finished* before the restart has no other
    entry, and all it has is of the first attempt. One that the restart cut short may have one more entry, for what
    its turn had spent by then, and that entry is of an earlier attempt than the turn it ended with.
    """
    found = []
    for key in agents:
        entries = [(turn, attempt) for entry_key, turn, attempt, _usage in ledger if entry_key == key]
        ended = [attempt for turn, attempt in entries if not turn.startswith(_CUT_SHORT)]
        partial = [attempt for turn, attempt in entries if turn.startswith(_CUT_SHORT)]
        if len(ended) != 1:
            found.append(f"{key} has {len(ended)} entries for the turn it ended with: {entries}")
        elif key in finished and (partial or ended != [1]):
            found.append(f"{key} had finished before the restart and has more than one entry of attempt 1: {entries}")
        elif len(partial) > 1 or any(attempt >= ended[0] for attempt in partial):
            found.append(f"{key} was cut short and has more than one partial entry before its last turn: {entries}")
    return found


def _spent(entries: list[dict[str, Any]], *, cost: bool) -> float:
    """Sum what *entries* used: their cost, or their tokens when a turn has no price."""
    if cost:
        return sum(float(entry["cost_usd"]) for entry in entries)
    return sum(float(entry.get("input_tokens") or 0) + float(entry.get("output_tokens") or 0) for entry in entries)


def main() -> int:
    """Run the restart once and print its result line; exit 0 when it is the expected one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--after", type=int, default=3, help="restart once this many research agents have completed")
    parser.add_argument("--sleep", type=int, default=20, help="seconds each research agent sleeps before it answers")
    parser.add_argument("--slot", type=int, help="the live deployment's slot (default: the only ready one)")
    add_send_options(parser)
    parser.add_argument("--settle", type=float, default=90, help="seconds to wait for reports after the run ended")
    args = parser.parse_args()
    dev = args.dev if args.send == "ask" else None
    deployment = Deployment.find(args.slot)
    token = f"tide{secrets.token_hex(6)}"
    # Only the run's result holds this word: no prompt and no agent's answer does, so no other reply repeats it.
    report = f"harbor{secrets.token_hex(6)}"

    backend = deployment.backend()
    # A running backend reloads the edit some time later; one started after it has the toolkit before it is asked.
    if deployment.give_workflow_toolkit() or "MINDROOM_WORKFLOW_FAULT_DIR" in backend.environment:
        backend.stop()
        backend.start()
    source = _DIAMOND.format(token=token, topics="\\n".join(_TOPICS), sleep=args.sleep, report=report)
    root = start_run(deployment, source, token, dev)

    def completed_calls() -> set[str]:
        found = deployment.run(token)
        return {str(node["key"]) for node in found[1] if node["state"] == "completed"} if found else set()

    def enough_done() -> bool:
        found = deployment.run(token)
        research = [node for node in found[1] if node["kind"] == "agent" and node["name"] in _TOPICS] if found else []
        return sum(node["state"] == "completed" for node in research) >= args.after

    until(f"{args.after} completed research agents", lambda: enough_done() or None, 900, 1)
    say(f"{args.after} research agents completed; restarting the backend (process {backend.pid})")
    # Read before the stop is asked for: an agent that completes while the backend stops counts as cut short here,
    # which only allows it an entry it does not have.
    finished = completed_calls()
    backend.stop()
    backend.start()
    say("the backend is back; waiting for the run to end")
    run, nodes = deployment.ended_run(token, 1800)
    time.sleep(args.settle)
    agents = [node for node in nodes if node["kind"] == "agent"]
    completed = sum(node["state"] == "completed" and node["generation"] == 1 for node in agents)
    subagents = {json.loads(node["links_json"]).get("subagent_id") for node in agents}
    ledger = _ledger(deployment, str(run["run_id"]))
    priced = all(entry.get("cost_usd") is not None for *_, entry in ledger)
    whole = _spent([entry for *_, entry in ledger], cost=priced)
    last = _spent([entry for *_, attempt, entry in ledger if attempt == run["attempt"]], cost=priced)
    say(f"the run used {whole:g} and its last attempt {last:g} ({'USD' if priced else 'tokens'})")
    twice = _paid_twice(ledger, [str(node["key"]) for node in agents], finished)
    partial = sum(turn.startswith(_CUT_SHORT) for _, turn, *_ in ledger)
    say(f"the ledger has {len(ledger)} entries, {partial} of them for a turn the restart cut short")
    for finding in twice:
        say(finding)
    line = (
        f"agents={completed}/{len(agents)} subagents={len(subagents)} paid_once={not twice} "
        f"attempt_below_run={last < whole} {outcome(deployment, run)} reports={deployment.replies_with(root, report)}"
    )
    print(line, flush=True)
    return 0 if line == _EXPECTED else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
