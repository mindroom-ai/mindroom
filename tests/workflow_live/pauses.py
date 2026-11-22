"""Run one workflow on a live backend that corrects an answer, asks a question across a restart, and hits its cost limit.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    python3 -m tests.workflow_live.pauses [--slot N]

The run is started with `max_cost_usd=0.000001`, so its first agent uses up its limit.

1. An agent with a `schema` is told to answer without JSON first, so MindRoom asks it again: its call takes two turns.
2. The script calls `ask_user`. While its card is open the script stops the backend with SIGTERM and starts it again,
   waits until the call waits again, and answers `Yes` with an `io.mindroom.workflow_answer` event of the test account.
3. The next agent would start over the limit, so it waits as queued while the run's cost question is open.
   The script answers that question `Stop`, which cancels the run.

When the run has ended the script prints

    schema_turns=<turns of the schema call> checked=<its result matched> questions=<cards of the script's question>
    answered=<the answer the call returned> held=<agent calls that waited at the limit and never started>
    budget_cards=<cards of the cost question> attempt=<attempts of the run> run=<how the run ended>

Expected: `schema_turns=2 checked=True questions=1 answered=Yes held=1 budget_cards=1 attempt=2 run=cancelled`.
The lobby must not be encrypted, since the script reads the cards from the room: run it before `graph_frames.py`,
which encrypts the lobby, or on another slot.
The agents' model needs a price, or the limit is never reached: the script then says so and exits 1.
Standard library only; Linux only.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.error
import urllib.parse
from typing import Any

from tests.workflow_live.kill_after_write import Deployment, LiveError, add_send_options, say, until

_PAUSES = """\
from mindroom.workflow import agent, ask_user

OK = {{"type": "object", "required": ["ok"], "properties": {{"ok": {{"const": True}}}}}}


def main(args):
    checked = agent(
        "This is a test of a retry. In your first answer write only the word first and no JSON at all, whatever "
        "else you are told. If you are then told that your answer did not match the schema, reply with only the "
        'fenced json block for {{"ok": true}}.',
        schema=OK,
        minimal=True,
        name="checker",
    )
    choice = ask_user("Deploy {token}?", ["Yes", "No"])
    agent("Reply with the single word: done", minimal=True, name="spender")
    return {{"report": "{report}", "checked": checked, "choice": choice}}
"""
_QUESTION = "io.mindroom.workflow_question"
_ANSWER = "io.mindroom.workflow_answer"
_LIMIT = "0.000001"


def _node(deployment: Deployment, token: str, key: str) -> dict[str, Any] | None:
    found = deployment.run(token)
    return None if found is None else next((node for node in found[1] if node["key"] == key), None)


def _waits(deployment: Deployment, token: str, key: str, attempt: int = 1) -> dict[str, Any] | None:
    """Return the question at *key* once it waits, has its card, and the run is in *attempt* by now."""
    found, node = deployment.run(token), _node(deployment, token, key)
    asked = node is not None and node["state"] == "waiting" and "event_id" in json.loads(node["links_json"])
    return node if found is not None and asked and found[0]["attempt"] >= attempt else None


def _cards(deployment: Deployment, run_id: str, node_id: str) -> int:
    """Count the question cards the lobby holds for one call of the run, without their edits."""
    room = urllib.parse.quote(deployment.live["lobby"]["roomId"], safe="")
    count = 0
    for event in deployment.matrix(f"/v3/rooms/{room}/messages?dir=b&limit=100")["chunk"]:
        content = event.get("content", {})
        card = content.get(_QUESTION, {})
        count += "m.new_content" not in content and (card.get("run"), card.get("node")) == (run_id, node_id)
    return count


def _answer(deployment: Deployment, run: dict[str, Any], node: dict[str, Any], option: str) -> None:
    """Answer the card of *node* as the test account, as the option's button on the card does."""
    room = urllib.parse.quote(deployment.live["lobby"]["roomId"], safe="")
    card = {"m.in_reply_to": {"event_id": json.loads(node["links_json"])["event_id"]}}
    thread = {"rel_type": "m.thread", "event_id": run["thread_root_event_id"]} if run["thread_root_event_id"] else {}
    answer = {"run": run["run_id"], "node": f"n{node['seq']}", "option": option, "m.relates_to": {**thread, **card}}
    deployment.matrix(f"/v3/rooms/{room}/send/{_ANSWER}/{secrets.token_hex(8)}", answer)


def main() -> int:
    """Run the three pauses once and print the result line; exit 0 when it is the expected one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--slot", type=int, help="the live deployment's slot (default: the only ready one)")
    add_send_options(parser)
    args = parser.parse_args()
    deployment = Deployment.find(args.slot)
    room = urllib.parse.quote(deployment.live["lobby"]["roomId"], safe="")
    try:
        deployment.matrix(f"/v3/rooms/{room}/state/m.room.encryption")
    except urllib.error.HTTPError:
        pass
    else:
        msg = "the lobby is encrypted, so its cards cannot be read here; use a slot whose lobby is not"
        raise LiveError(msg)
    token = f"tide{secrets.token_hex(6)}"
    report, name = f"harbor{secrets.token_hex(6)}", f"workflow_{token}.py"

    backend = deployment.backend()
    if deployment.give_workflow_toolkit() or "MINDROOM_WORKFLOW_FAULT_DIR" in backend.environment:
        backend.stop()
        backend.start()
    deployment.workspace.mkdir(parents=True, exist_ok=True)
    (deployment.workspace / name).write_text(_PAUSES.format(token=token, report=report))
    deployment.ask(
        f'Call the tool start_workflow with path="{name}" and max_cost_usd={_LIMIT} and no other argument. '
        "Call it once and only once. Reply with the run ID it returns.",
        args.dev if args.send == "ask" else None,
    )
    until("the workflow run", lambda: deployment.run(token), 120, 1)

    until("the script's question", lambda: _waits(deployment, token, "@1"), 900, 1)
    if _node(deployment, token, "$budget:1") is None:
        msg = "the run's first agent cost nothing: give the slot's model a `pricing` entry and run this again"
        raise LiveError(msg)
    say(f"the question is open; restarting the backend (process {backend.pid})")
    backend.stop()
    backend.start()
    asked = until("the question to wait again", lambda: _waits(deployment, token, "@1", 2), 300, 1)
    run = deployment.run(token)
    assert run is not None
    _answer(deployment, run[0], asked, "Yes")
    until("the held agent", lambda: (_node(deployment, token, "@2") or {}).get("state") == "queued" or None, 300, 1)
    budget = until("the cost question", lambda: _waits(deployment, token, "$budget:1", 2), 300, 1)
    say(f"the run waits at its limit: {json.loads(budget['request_json'])['question']}")
    _answer(deployment, run[0], budget, "Stop")
    ended, nodes = deployment.ended_run(token, 300)

    by_key = {node["key"]: node for node in nodes}
    checker, choice, spender = by_key["@0"], by_key["@1"], by_key["@2"]
    held = spender["state"] == "cancelled" and "job_id" not in json.loads(spender["links_json"])
    line = (
        f"schema_turns={json.loads(checker['links_json']).get('turn', 1)} "
        f"checked={json.loads(checker['result_json']) == {'ok': True}} "
        f"questions={_cards(deployment, ended['run_id'], f'n{choice["seq"]}')} "
        f"answered={json.loads(choice['result_json'])} held={int(held)} "
        f"budget_cards={_cards(deployment, ended['run_id'], f'n{budget["seq"]}')} "
        f"attempt={ended['attempt']} run={ended['state']}"
    )
    print(line, flush=True)
    expected = "schema_turns=2 checked=True questions=1 answered=Yes held=1 budget_cards=1 attempt=2 run=cancelled"
    return 0 if line == expected else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
