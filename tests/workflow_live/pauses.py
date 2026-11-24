"""Run two workflows on a live backend: one corrects an answer, one asks a question across a restart and hits its cost limit.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    python3 -m tests.workflow_live.pauses [--slot N]

1. The first run has no cost limit.
   Its agent has a `schema` that no value matches, so MindRoom asks it again after every answer: its call takes the
   five turns a call gets and then fails with kind `schema`, which the script catches.
2. The second run is started with `max_cost_usd=0.000001`, so its first agent uses up its limit.
   Its script calls `ask_user` first. While the card is open this script stops the backend with SIGTERM and starts
   it again, waits until the call waits again, and answers `Yes` with an `io.mindroom.workflow_answer` event of the
   test account.
3. Then one agent of that run answers and spends past the limit, so the next agent never starts and the run ends
   `interrupted` with `Stopped at the cost limit (...)`.

A further turn of a `schema` call is new spending and stops at the limit too, which is why the correction has a run
of its own.
When both runs have ended the script prints

    schema_turns=<turns of the schema call> schema_kind=<how it ended> questions=<cards of the script's question>
    answered=<the answer the call returned> held=<1 when the second agent never started a job>
    limit_reason=<True when the run's error reports the cost limit> attempt=<attempts of the second run>
    run=<how the second run ended>

Expected: `schema_turns=5 schema_kind=schema questions=1 answered=Yes held=1 limit_reason=True attempt=2 run=interrupted`.
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

from tests.workflow_live.kill_after_write import Deployment, LiveError, add_send_options, say, start_run, until

_CORRECTED = """\
from mindroom.workflow import AgentError, agent

# No value matches: `ok` would have to be true and a string. So every answer is corrected, whatever the model does.
NEVER = {{"type": "object", "required": ["ok"], "properties": {{"ok": {{"const": True, "type": "string"}}}}}}


def main(args):
    try:
        agent('Reply with only the fenced json block for {{"ok": true}}. Call no tool.', schema=NEVER, name="checker")
    except AgentError as error:
        return {{"report": "{report}", "token": "{token}", "kind": error.kind}}
    return {{"report": "{report}", "token": "{token}", "kind": "completed"}}
"""
_PAUSES = """\
from mindroom.workflow import agent, ask_user


def main(args):
    choice = ask_user("Deploy {token}?", ["Yes", "No"])
    agent("Reply with the single word: done", name="first")
    agent("Reply with the single word: done", name="spender")
    return {{"report": "{report}", "choice": choice}}
"""
_QUESTION = "io.mindroom.workflow_question"
_ANSWER = "io.mindroom.workflow_answer"
_LIMIT = "0.000001"
_ENDED = ("completed", "failed", "cancelled", "interrupted")


def _node(deployment: Deployment, token: str, key: str) -> dict[str, Any] | None:
    found = deployment.run(token)
    return None if found is None else next((node for node in found[1] if node["key"] == key), None)


def _waits(deployment: Deployment, token: str, key: str, attempt: int = 1) -> dict[str, Any] | None:
    """Return the question at *key* once it waits, has its card, and the run is in *attempt* by now."""
    found, node = deployment.run(token), _node(deployment, token, key)
    if found is not None and found[0]["state"] in _ENDED:
        msg = f"the run ended {found[0]['state']} before its question at {key} waited: {found[0]['error']}"
        raise LiveError(msg)
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
    dev = args.dev if args.send == "ask" else None
    deployment = Deployment.find(args.slot)
    if deployment.lobby_encrypted():
        msg = "the lobby is encrypted, so its cards cannot be read here; use a slot whose lobby is not"
        raise LiveError(msg)
    corrected, token = f"reef{secrets.token_hex(6)}", f"tide{secrets.token_hex(6)}"
    report, name = f"harbor{secrets.token_hex(6)}", f"workflow_{token}.py"

    backend = deployment.backend()
    if deployment.give_workflow_toolkit() or "MINDROOM_WORKFLOW_FAULT_DIR" in backend.environment:
        backend.stop()
        backend.start()
    start_run(deployment, _CORRECTED.format(token=corrected, report=report), corrected, dev)
    ended, (checker,) = deployment.ended_run(corrected, 900)
    kind = (json.loads(ended["result_json"] or "null") or {}).get("kind")
    say(f"the schema call ended {checker['state']}; starting the run with the limit")
    (deployment.workspace / name).write_text(_PAUSES.format(token=token, report=report))
    deployment.ask(
        f'Call the tool start_workflow with path="{name}" and max_cost_usd={_LIMIT} and no other argument. '
        "Call it once and only once. Reply with the run ID it returns.",
        dev,
    )
    until("the workflow run", lambda: deployment.run(token), 120, 1)

    until("the script's question", lambda: _waits(deployment, token, "@0"), 300, 1)
    say(f"the question is open; restarting the backend (process {backend.pid})")
    backend.stop()
    backend.start()
    asked = until("the question to wait again", lambda: _waits(deployment, token, "@0", 2), 300, 1)
    run = deployment.run(token)
    assert run is not None
    _answer(deployment, run[0], asked, "Yes")
    ended, nodes = deployment.ended_run(token, 600)
    if ended["state"] == "completed":
        msg = "the run's first agent cost nothing: give the slot's model a `pricing` entry and run this again"
        raise LiveError(msg)
    say(f"the run ended {ended['state']}: {ended['error']}")

    by_key = {node["key"]: node for node in nodes}
    choice, spender = by_key["@0"], by_key.get("@2")
    held = spender is None or (spender["state"] == "cancelled" and "job_id" not in json.loads(spender["links_json"]))
    limit_reason = (ended["error"] or "").startswith("Stopped at the cost limit (")
    line = (
        f"schema_turns={json.loads(checker['links_json']).get('turn', 1)} schema_kind={kind} "
        f"questions={_cards(deployment, ended['run_id'], f'n{choice["seq"]}')} "
        f"answered={json.loads(choice['result_json'])} held={int(held)} "
        f"limit_reason={limit_reason} attempt={ended['attempt']} run={ended['state']}"
    )
    print(line, flush=True)
    expected = (
        "schema_turns=5 schema_kind=schema questions=1 answered=Yes held=1 limit_reason=True attempt=2 run=interrupted"
    )
    return 0 if line == expected else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
